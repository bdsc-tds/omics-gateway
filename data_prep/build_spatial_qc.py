"""
Script to build spatial dataset's QC folder: metrics table plus 10x reports.

Writes <qc-data>/spatial/<dataset-id>/metrics.json, one row per sample from
10x metrics_summary.csv, and copies each sample's 10x report into reports/
with absolute paths reduced to file names (Space Ranger and Xenium Ranger
record their command lines, cluster paths included). Gateway shows table at
/qc/<dataset-id>, so dataset id must match datasets.tsv for catalogue's QC
button to appear.

Each --sample names sample key and folder holding its report and
metrics_summary.csv: Space Ranger's outs/ (web_summary.html) or Xenium bundle
(analysis_summary.html). Store gives sample descriptions (disease) and, for
Visium HD, cells kept per sample: Space Ranger counts every segmented cell,
store only QC-passed ones.

Usage:
    conda run -n spatial python data_prep/build_spatial_qc.py \
        --dataset-id visium_hd_skin \
        --store data/spatial/visium_hd/visium_hd_skin.zarr \
        --sample S1=<outs of S1> --sample S2=<outs of S2>
"""

# Import utility modules
import argparse
import csv
import json
import os
import re
import shutil
import sys

# Import analysis modules
import zarr

# Import sibling script; data_prep/ is on sys.path when scripts run directly
from generate_datasets_tsv import TABLE_PATH, extract_zarr_metadata

# Report file and metrics table each technology's pipeline writes
REPORT_FILES = {
    'visium_hd': 'web_summary.html',
    'xenium': 'analysis_summary.html',
}
METRICS_FILE = 'metrics_summary.csv'

# Table columns per technology: (group label, [(key, label, CSV column,
# format, extra column fields)])
COLUMNS = {
    'visium_hd': [
        (
            'Sequencing',
            [
                ('reads', 'Reads', 'Number of Reads', 'millions', {}),
                (
                    'saturation',
                    'Saturation',
                    'Sequencing Saturation',
                    'percent',
                    {},
                ),
                (
                    'reads_under_tissue',
                    'Reads under tissue',
                    'Fraction Reads in Squares Under Tissue',
                    'percent',
                    {},
                ),
            ],
        ),
        (
            'Cells segmented by Space Ranger',
            [
                ('cells', 'Cells', 'Number of Cells', 'integer', {}),
                (
                    'median_umis',
                    'Median UMIs',
                    'Median UMIs per Cell',
                    'integer',
                    {},
                ),
                (
                    'median_genes',
                    'Median genes',
                    'Median Genes per Cell',
                    'integer',
                    {},
                ),
                (
                    'median_area',
                    'Median area',
                    'Median Cell Area (μm²)',
                    'integer',
                    {'unit': 'µm²'},
                ),
            ],
        ),
    ],
    'xenium': [
        (
            'Transcripts',
            [
                (
                    'decoded_density',
                    'Decoded / 100 µm²',
                    'decoded_transcripts_per_100um2',
                    'integer',
                    {},
                ),
                (
                    'q20',
                    'Q20',
                    'fraction_transcripts_decoded_q20',
                    'percent',
                    {},
                ),
                (
                    'in_cells',
                    'In cells',
                    'fraction_transcripts_assigned',
                    'percent',
                    {},
                ),
                (
                    'negative_control_rate',
                    'Neg. control rate',
                    'adjusted_negative_control_probe_rate',
                    'percent',
                    {'digits': 2},
                ),
            ],
        ),
        (
            'Cells',
            [
                ('cells', 'Cells', 'num_cells_detected', 'integer', {}),
                (
                    'median_transcripts',
                    'Median transcripts',
                    'median_transcripts_per_cell',
                    'integer',
                    {},
                ),
                (
                    'median_genes',
                    'Median genes',
                    'median_genes_per_cell',
                    'integer',
                    {},
                ),
            ],
        ),
    ],
}
NOTES = {
    'visium_hd': 'Space Ranger counts every cell it segments; the viewer '
    'keeps QC-passed cells only, hence two cell counts.',
    'xenium': None,
}

# Absolute paths in reports, plain or JSON-escaped; lookbehind skips URLs
ROOTS = r'(?:work|data|home|users|scratch|mnt|tmp|opt|storage|cluster)'
PLAIN_PATH = re.compile(rf'(?<![\w.:/\\])/{ROOTS}/[^\s"\'<>\\]+')
ESCAPED_PATH = re.compile(rf'(?<![\w.:/\\])\\/{ROOTS}\\/(?:[^\s"\'<>\\]|\\/)+')
# Names that must never survive, whatever path form held them
LEFTOVER_TOKENS = ('athieba1', 'rgottar1', 'PRTNR', 'xenium_paper')


# Function to find sample folder's technology from its report
def detect_technology(sample_dir):
    """
    Tell Visium HD and Xenium sample folders apart by report they hold.

    Parameters:
    -----------
    sample_dir: str
      Folder holding sample's 10x report and metrics_summary.csv.

    Returns:
    --------
    technology: str
      Key of REPORT_FILES.
    """
    found = [
        tech
        for tech, name in REPORT_FILES.items()
        if os.path.isfile(os.path.join(sample_dir, name))
    ]
    if len(found) != 1 or not os.path.isfile(
        os.path.join(sample_dir, METRICS_FILE)
    ):
        sys.exit(
            f'{sample_dir}: expected {METRICS_FILE} and one of '
            f'{", ".join(REPORT_FILES.values())}'
        )
    return found[0]


# Function to read sample's metrics_summary.csv
def read_metrics(sample_dir):
    """
    Read single-row metrics_summary.csv into numbers.

    Parameters:
    -----------
    sample_dir: str
      Folder holding metrics_summary.csv.

    Returns:
    --------
    metrics: dict
      CSV column to number; empty cells left out.
    """
    with open(os.path.join(sample_dir, METRICS_FILE), newline='') as handle:
        row = next(csv.DictReader(handle))
    metrics = {}
    for column, text in row.items():
        try:
            value = float(text)
        except (TypeError, ValueError):
            continue
        # Counts stay integers in metrics.json
        metrics[column] = int(value) if value.is_integer() else value
    return metrics


# Function to strip cluster paths from report
def scrub_report(text, source):
    """
    Reduce absolute paths in report to file names, then refuse report if any
    path or cluster name is left.

    Parameters:
    -----------
    text: str
      Report HTML.
    source: str
      Report path, for error message.

    Returns:
    --------
    scrubbed: str
      Report HTML without absolute paths.
    """
    scrubbed = PLAIN_PATH.sub(
        lambda m: m.group(0).rstrip('/').rsplit('/', 1)[-1], text
    )
    scrubbed = ESCAPED_PATH.sub(
        lambda m: m.group(0).rstrip('/').rstrip('\\').rsplit('\\/', 1)[-1],
        scrubbed,
    )
    leftovers = PLAIN_PATH.findall(scrubbed) + ESCAPED_PATH.findall(scrubbed)
    leftovers += [token for token in LEFTOVER_TOKENS if token in scrubbed]
    if leftovers:
        sys.exit(f'{source}: paths left after scrubbing: {leftovers[:5]}')
    return scrubbed


# Function to read per-sample cell counts and diseases from store
def read_store_samples(store_path):
    """
    Read cells kept and disease per sample from store's table, loading only
    those obs columns.

    Parameters:
    -----------
    store_path: str
      Path to .zarr store.

    Returns:
    --------
    samples: dict
      Sample key to {'cells': int, 'disease': str}; empty for single-sample
      store, whose table has no sample column.
    """
    from anndata.io import read_elem

    obs = zarr.open_group(os.path.join(store_path, TABLE_PATH), mode='r')['obs']
    if 'sample' not in obs:
        return {}
    sample = read_elem(obs['sample'])
    disease = read_elem(obs['disease']) if 'disease' in obs else None
    samples = {}
    for key in sample.categories:
        mask = sample == key
        samples[str(key)] = {
            'cells': int(mask.sum()),
            'disease': str(disease[mask][0]) if disease is not None else '',
        }
    return samples


# Function to build metrics.json row for one sample
def build_row(key, sample_dir, technology, store_sample, description):
    """
    Assemble table row from sample's metrics and store's counts.

    Parameters:
    -----------
    key: str
      Sample key, shown as row name and naming its report copy.
    sample_dir: str
      Folder holding sample's report and metrics_summary.csv.
    technology: str
      Key of COLUMNS.
    store_sample: dict or None
      Sample's entry from read_store_samples, None for single-sample store.
    description: str
      Line shown under sample name.

    Returns:
    --------
    row: dict
      metrics.json row.
    """
    metrics = read_metrics(sample_dir)
    values = {}
    for _, columns in COLUMNS[technology]:
        for column_key, _, source, _, _ in columns:
            if source not in metrics:
                print(f'  Warning: {key}: no {source!r} in {METRICS_FILE}')
            values[column_key] = metrics.get(source)
    if store_sample is not None:
        values['cells_in_viewer'] = store_sample['cells']
    return {
        'sample': key,
        'description': description,
        'values': values,
        'report': f'reports/{key}.html',
    }


# Function to build metrics.json groups for technology
def build_groups(technology, per_sample_store):
    """
    Turn COLUMNS entry into metrics.json groups.

    Parameters:
    -----------
    technology: str
      Key of COLUMNS.
    per_sample_store: bool
      Whether store counts cells per sample, adding 'Cells in viewer'.

    Returns:
    --------
    groups: list of dict
      metrics.json groups.
    """
    groups = [
        {
            'label': label,
            'columns': [
                {
                    'key': key,
                    'label': column_label,
                    'source': source,
                    'format': fmt,
                    **extra,
                }
                for key, column_label, source, fmt, extra in columns
            ],
        }
        for label, columns in COLUMNS[technology]
    ]
    if per_sample_store:
        groups.append({
            'label': None,
            'columns': [
                {
                    'key': 'cells_in_viewer',
                    'label': 'Cells in viewer',
                    'source': 'QC-passed cells kept in the viewer',
                    'format': 'integer',
                }
            ],
        })
    return groups


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments.

    Parameters:
    -----------
    None

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Build spatial dataset QC folder from 10x outputs.'
    )
    parser.add_argument(
        '--dataset-id',
        required=True,
        help='QC folder name, as dataset_id in datasets.tsv.',
    )
    parser.add_argument(
        '--store', required=True, help='SpatialData .zarr store of dataset.'
    )
    parser.add_argument(
        '--sample',
        action='append',
        required=True,
        metavar='KEY=DIR',
        help='Sample key and folder holding its 10x report and '
        'metrics_summary.csv; repeat per sample, in table order.',
    )
    parser.add_argument(
        '--description',
        default='',
        help='Line under sample names when store records no disease.',
    )
    parser.add_argument(
        '--qc-data',
        default=os.environ.get('QC_DATA', 'data/qc'),
        help='QC root directory (default: $QC_DATA or data/qc).',
    )
    return parser.parse_args()


# Function to build QC folder
def main():
    """
    Build metrics.json and scrubbed report copies for every sample.

    Parameters:
    -----------
    None

    Returns:
    --------
    None
    """
    args = parse_args()
    # Output folder is deleted before rebuild, so id must name one folder
    if args.dataset_id in ('', '.', '..') or '/' in args.dataset_id:
        sys.exit(f'--dataset-id {args.dataset_id!r}: expected folder name')
    samples = []
    for spec in args.sample:
        key, sep, sample_dir = spec.partition('=')
        if not sep or not key or '/' in key:
            sys.exit(f'--sample {spec!r}: expected KEY=DIR')
        samples.append((key, sample_dir))
    technologies = {detect_technology(d) for _, d in samples}
    if len(technologies) != 1:
        sys.exit(f'Samples mix technologies: {sorted(technologies)}')
    technology = technologies.pop()

    store_samples = read_store_samples(args.store)
    store_disease = extract_zarr_metadata(args.store)['disease']
    missing = [
        key for key, _ in samples if store_samples and key not in store_samples
    ]
    if missing:
        sys.exit(f'Samples absent from store: {missing}')

    out_dir = os.path.join(args.qc_data, 'spatial', args.dataset_id)
    # Folder holds only what this script writes, so rebuild it whole
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, 'reports'))

    rows = []
    for key, sample_dir in samples:
        store_sample = store_samples.get(key) if store_samples else None
        description = (
            (store_sample or {}).get('disease')
            or store_disease
            or args.description
        )
        rows.append(
            build_row(key, sample_dir, technology, store_sample, description)
        )
        source = os.path.join(sample_dir, REPORT_FILES[technology])
        with open(source, encoding='utf-8') as handle:
            report = scrub_report(handle.read(), source)
        with open(
            os.path.join(out_dir, 'reports', f'{key}.html'),
            'w',
            encoding='utf-8',
            newline='',
        ) as handle:
            handle.write(report)
        print(f'  {key}: report copied')

    metrics = {
        'title': 'Sample metrics',
        'note': NOTES[technology],
        'groups': build_groups(technology, bool(store_samples)),
        'rows': rows,
    }
    with open(
        os.path.join(out_dir, 'metrics.json'), 'w', encoding='utf-8', newline=''
    ) as handle:
        json.dump(metrics, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    print(f'Wrote {out_dir} ({len(rows)} samples)')


if __name__ == '__main__':
    main()
