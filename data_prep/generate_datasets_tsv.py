"""
Script to generate datasets.tsv from .h5ad files and SpatialData stores.

Rows come from each dataset's uns metadata: .h5ad files carry it from team
pipeline, and write_store_metadata.py writes same keys into .zarr stores.

Usage:
    python data_prep/generate_datasets_tsv.py \
        --data-dir data/ \
        --output datasets.tsv \
        --merged-file data/meta_analysis_all_final_label_transfer_swapped.h5ad \
        --merged-config data_prep/meta_analysis_config.yaml
"""

# Import utility modules
import argparse
import csv
import os
import re

import yaml

# Define columns for output TSV file
TSV_COLUMNS = [
    'dataset_id',
    'name',
    'description',
    'file_path',
    'assay',
    'disease',
    'tissue',
    'sex',
    'patients',
    'cell_count',
    'gene_count',
    'year',
    'authors',
    'journal',
    'doi',
]

# uns key read for each TSV column; year is extracted from article_date. Also
# defines keys write_store_metadata.py accepts
UNS_KEYS = {
    'dataset_id': 'dataset_name_short',
    'name': 'dataset_name',
    'description': 'article_title',
    'assay': 'assay',
    'disease': 'disease',
    'tissue': 'tissue',
    'sex': 'sex',
    'patients': 'patients',
    'cell_count': 'cell_count',
    'gene_count': 'gene_count',
    'year': 'article_date',
    'authors': 'article_authors',
    'journal': 'article_journal',
    'doi': 'article_doi',
}

# Only list columns split comma-separated strings, so titles keep their commas
LIST_COLUMNS = {'assay', 'disease', 'tissue', 'sex', 'authors'}

# AnnData table inside SpatialData store
TABLE_PATH = 'tables/table'


# Function to extract year from date string using regex
def extract_year(date_str):
    """
    Extract 4-digit year from date string.

    Parameters:
    -----------
    date_str: str
      Date string potentially containing year.

    Returns:
    --------
    year: str
      4-digit year string, or empty string if none found.
    """
    if not date_str:
        return ''
    match = re.search(r'\b(19|20)\d{2}\b', str(date_str))

    return match.group(0) if match else ''


# Function to read field from uns dict
def read_uns_field(uns, key, split_commas=True):
    """
    Safely read field from AnnData uns dict, handling lists/arrays. Always
    returns semicolon-separated string to keep TSV consistent.

    Parameters:
    -----------
    uns: dict
      AnnData uns dictionary.
    key: str
      Key to retrieve.
    split_commas: bool
      Whether to turn comma-separated string into semicolon-separated one;
      off for free text such as titles.

    Returns:
    --------
    value: str
      Semicolon-separated string of values, or empty string if missing.
    """
    val = uns.get(key, '')
    if val is None:
        return ''
    if hasattr(val, 'tolist'):
        val = val.tolist()
    if isinstance(val, list):
        return '; '.join(str(v).strip() for v in val)
    # Normalise comma-separated strings (from uns metadata) to semicolons
    text = str(val).strip()
    if split_commas and ', ' in text and ';' not in text:
        return '; '.join(v.strip() for v in text.split(', '))

    return text


# Function to sort semicolon-separated field
def sort_semicolon_field(value, fixed_order=None):
    """
    Sort semicolon-separated field alphabetically, with NA values last.

    Parameters:
    -----------
    value: str
      Semicolon-separated string.
    fixed_order: list or None
      If provided, sort tokens by their position in this list; tokens absent
      from it sort alphabetically after listed ones.

    Returns:
    --------
    sorted_value: str
      Re-joined semicolon-separated string with NAs last, or empty string if
      all tokens were NA/empty.
    """
    if not value:
        return ''
    tokens = [t.strip() for t in value.split(';') if t.strip()]
    if not tokens:
        return ''

    NA_VALUES = {'na', 'n/a', 'nan', 'none'}

    def sort_key(token):
        """
        Build sort key placing NA tokens last, then by fixed order or name.

        Parameters:
        -----------
        token: str
          Stripped token from semicolon-separated field.

        Returns:
        --------
        key: tuple
          (NA flag, position in fixed_order, lower-cased token) when fixed order
          is given, otherwise (NA flag, lower-cased token).
        """
        token_lower = token.lower()
        is_na = token_lower in NA_VALUES
        if fixed_order is not None:
            try:
                pos = [f.lower() for f in fixed_order].index(token_lower)
            except ValueError:
                pos = len(fixed_order)
            return (1 if is_na else 0, pos, token_lower)
        return (1 if is_na else 0, token_lower)

    tokens.sort(key=sort_key)

    return '; '.join(tokens)


# Function to strip specific tokens from semicolon-separated field
def strip_tokens(value, to_strip):
    """
    Remove specific tokens (case-insensitive) from semicolon-separated field.

    Parameters:
    -----------
    value: str
      Semicolon-separated string.
    to_strip: set of str
      Token values to remove (compared case-insensitively).

    Returns:
    --------
    cleaned: str
      Re-joined semicolon-separated string without those tokens, or empty
      string if all tokens were removed.
    """
    if not value:
        return ''
    to_strip_lower = {v.lower() for v in to_strip}
    tokens = [
        t.strip()
        for t in value.split(';')
        if t.strip() and t.strip().lower() not in to_strip_lower
    ]

    return '; '.join(tokens)


# Function to build metadata dict with every column empty
def empty_metadata(name):
    """
    Build metadata dict for dataset whose metadata cannot be read.

    Parameters:
    -----------
    name: str
      Fallback dataset name, usually file name without extension.

    Returns:
    --------
    meta: dict
      Dictionary with every TSV column but file_path, all empty except name.
    """
    meta = {column: '' for column in UNS_KEYS}
    meta['name'] = name
    return meta


# Function to count rows of AnnData obs or var group
def index_length(group):
    """
    Count entries of AnnData dataframe group from its index, without loading
    columns.

    Parameters:
    -----------
    group: h5py.Group or zarr.Group
      AnnData obs or var group.

    Returns:
    --------
    length: int
      Number of entries.
    """
    index = group[group.attrs['_index']]
    if hasattr(index, 'shape'):
        return index.shape[0]
    # Categorical or nullable index is group of arrays, not array
    for key in ('codes', 'values'):
        if key in index:
            return index[key].shape[0]
    raise ValueError(f'Unknown index encoding: {dict(index.attrs)}')


# Function to read metadata from AnnData table without loading data
def read_table_metadata(group, name):
    """
    Read dataset metadata from AnnData table's uns keys and index lengths.

    Only keys in UNS_KEYS are read, so cost stays small even for large tables:
    loading whole file (even backed) once exhausted machine memory.

    Parameters:
    -----------
    group: h5py.File or zarr.Group
      Root group of AnnData table (.h5ad file or table inside store).
    name: str
      Fallback dataset name when uns has none.

    Returns:
    --------
    meta: dict
      Dictionary with keys: dataset_id, name, description, assay, disease,
      tissue, sex, patients, cell_count, gene_count, year, authors, journal,
      doi. All values are strings.
    """
    from anndata.io import read_elem

    uns_group = group.get('uns', {})
    uns = {
        key: read_elem(uns_group[key])
        for key in UNS_KEYS.values()
        if key in uns_group
    }
    meta = {
        column: read_uns_field(uns, key, split_commas=column in LIST_COLUMNS)
        for column, key in UNS_KEYS.items()
    }
    meta['name'] = meta['name'] or name
    meta['year'] = extract_year(meta['year'])
    # Stores record no counts, so table size stands in when uns has none
    meta['cell_count'] = meta['cell_count'] or str(index_length(group['obs']))
    meta['gene_count'] = meta['gene_count'] or str(index_length(group['var']))

    return meta


# Function to extract metadata from .h5ad file's uns dict
def extract_h5ad_metadata(h5ad_path):
    """
    Extract dataset metadata from .h5ad file's uns dict.

    Parameters:
    -----------
    h5ad_path: str
      Absolute path to .h5ad file.

    Returns:
    --------
    meta: dict
      Dictionary with keys: dataset_id, name, description, assay, disease,
      tissue, sex, patients, cell_count, gene_count, year, authors, journal,
      doi. All values are strings.
    """
    filename_stem = os.path.splitext(os.path.basename(h5ad_path))[0]
    if not os.path.exists(h5ad_path):
        return empty_metadata(filename_stem)

    try:
        import h5py

        with h5py.File(h5ad_path, 'r') as handle:
            return read_table_metadata(handle, filename_stem)

    except Exception as e:  # noqa: BLE001
        # Broad by design: anndata/h5py raise undocumented errors on bad .h5ad;
        # warn and skip file rather than abort whole batch
        print(f'  Warning: could not read {h5ad_path}: {e}')
        return empty_metadata(filename_stem)


# Function to extract metadata from SpatialData store's table
def extract_zarr_metadata(store_path):
    """
    Extract dataset metadata from uns of SpatialData store's table.

    Parameters:
    -----------
    store_path: str
      Path to .zarr store.

    Returns:
    --------
    meta: dict
      Same keys as extract_h5ad_metadata. All values are strings.
    """
    stem = os.path.basename(os.path.normpath(store_path))[: -len('.zarr')]
    try:
        import zarr

        table = zarr.open_group(os.path.join(store_path, TABLE_PATH), mode='r')
        return read_table_metadata(table, stem)

    except Exception as e:  # noqa: BLE001
        # Broad for same reason as .h5ad reads: skip store, keep batch going
        print(f'  Warning: could not read {store_path}: {e}')
        return empty_metadata(stem)


# Function to recursively find .h5ad files and SpatialData stores
def find_dataset_files(data_dir):
    """
    Recursively find .h5ad files and SpatialData .zarr stores under data_dir.

    Stores are not descended into: they hold thousands of chunk files.

    Parameters:
    -----------
    data_dir: str
      Root directory to search.

    Returns:
    --------
    (h5ad_paths, zarr_paths): tuple of list of str
      Paths relative to data_dir, each list sorted. Only stores holding table
      are listed.
    """
    h5ad_paths, zarr_paths = [], []
    for dirpath, dirnames, filenames in os.walk(data_dir):
        for dirname in [d for d in dirnames if d.endswith('.zarr')]:
            dirnames.remove(dirname)
            full = os.path.join(dirpath, dirname)
            if os.path.isdir(os.path.join(full, TABLE_PATH)):
                zarr_paths.append(os.path.relpath(full, data_dir))
        for fname in filenames:
            if fname.endswith('.h5ad'):
                full = os.path.join(dirpath, fname)
                h5ad_paths.append(os.path.relpath(full, data_dir))

    return sorted(h5ad_paths), sorted(zarr_paths)


# Function to clean multi-value fields of metadata dict
def clean_fields(meta):
    """
    Strip placeholder tokens from disease, tissue and sex, then sort them.

    Parameters:
    -----------
    meta: dict
      Metadata dict, updated in place.

    Returns:
    --------
    meta: dict
      Same dict, for chaining.
    """
    meta['disease'] = sort_semicolon_field(
        strip_tokens(meta['disease'], {'healthy'})
    )
    meta['tissue'] = sort_semicolon_field(
        strip_tokens(meta['tissue'], {'na', 'n/a', 'nan', 'none'})
    )
    meta['sex'] = sort_semicolon_field(
        strip_tokens(meta['sex'], {'na', 'n/a', 'nan', 'none'}),
        fixed_order=['female', 'male'],
    )
    return meta


# Function to build merged dataset row from uns extraction and YAML overrides
def load_merged_row(merged_file, config_path):
    """
    Build TSV row dict for merged/meta-analysis .h5ad file.

    Parameters:
    -----------
    merged_file: str
      Path to merged .h5ad file.
    config_path: str or None
      Optional path to YAML file with manual field overrides.

    Returns:
    --------
    row: dict
      TSV row dict ready for csv.DictWriter, with file_path set to merged_file.
    """
    print(f'Processing merged file: {merged_file}')
    meta = extract_h5ad_metadata(merged_file)

    # Treat literal 'None' strings (from uns) as empty
    meta = {k: ('' if v == 'None' else v) for k, v in meta.items()}

    # Apply YAML overrides if config provided
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            overrides = yaml.safe_load(f) or {}
        field_map = {
            'name': 'name',
            'description': 'description',
            'authors': 'authors',
            'journal': 'journal',
            'doi': 'doi',
        }
        for cfg_key, meta_key in field_map.items():
            val = overrides.get(cfg_key, '')
            if val:
                meta[meta_key] = str(val)

    # Apply same post-processing as individual datasets
    clean_fields(meta)

    return {'file_path': merged_file, **meta}


# Function to generate datasets.tsv summary of .h5ad files
def generate_tsv(data_dir, output_path, merged_file=None, merged_config=None):
    """
    Generate datasets.tsv by scanning data_dir for .h5ad files and stores.

    Rows follow order: .h5ad files, merged file, then .zarr stores. Stores
    without dataset_name_short in their table metadata are skipped.

    Parameters:
    -----------
    data_dir: str
      Directory containing .h5ad files and .zarr stores (searched
      recursively).
    output_path: str
      Path for output .tsv file.
    merged_file: str or None
      Optional path to merged/meta-analysis .h5ad to append as last row.
    merged_config: str or None
      Optional path to YAML file with manual field overrides for merged row.
    """
    rows = []

    # Tell missing from empty directory: os.walk yields nothing for either, so
    # typo or unset GATEWAY_DATA would pass as directory without .h5ad files
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(
            f'Data directory {os.path.abspath(data_dir)} does not exist. '
            'Pass --data-dir or set GATEWAY_DATA.'
        )

    file_paths, zarr_paths = find_dataset_files(data_dir)
    if not file_paths and not zarr_paths:
        print(f'No .h5ad files or .zarr stores found in {data_dir}')
        return

    # Exclude merged file from individual dataset scan
    merged_relpath = (
        os.path.relpath(merged_file, data_dir)
        if merged_file and os.path.exists(merged_file)
        else None
    )

    for file_path in file_paths:
        if merged_relpath and os.path.normpath(file_path) == os.path.normpath(
            merged_relpath
        ):
            continue
        h5ad_path = os.path.join(data_dir, file_path)
        print(f'Processing: {file_path}')
        # Strip Healthy from disease; strip NA from tissue and sex
        meta = clean_fields(extract_h5ad_metadata(h5ad_path))

        out_row = {'file_path': file_path, **meta}
        rows.append(out_row)

    if merged_file and os.path.exists(merged_file):
        row = load_merged_row(merged_file, merged_config)
        row['file_path'] = merged_relpath
        rows.append(row)
    elif merged_file:
        print(f'Warning: merged file not found: {merged_file}')

    for store_path in zarr_paths:
        print(f'Processing: {store_path}')
        meta = clean_fields(
            extract_zarr_metadata(os.path.join(data_dir, store_path))
        )
        # Stores are listed only once given metadata, so scratch or reference
        # stores under data dir stay out of browser
        if not meta['dataset_id']:
            print(
                f'  Skipped: no {UNS_KEYS["dataset_id"]} in table metadata '
                '(see write_store_metadata.py)'
            )
            continue
        rows.append({'file_path': store_path, **meta})

    with open(output_path, 'w', newline='') as f:
        # Unix line endings: csv default CRLF would rewrite every line in diffs
        writer = csv.DictWriter(
            f, fieldnames=TSV_COLUMNS, delimiter='\t', lineterminator='\n'
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f'\nWrote {len(rows)} rows to {output_path}')


# Main function to parse arguments and create TSV
def main():
    """
    Function to parse arguments and run TSV generation.
    """
    parser = argparse.ArgumentParser(
        description='Generate datasets.tsv from .h5ad files in a directory.'
    )
    parser.add_argument(
        '--data-dir',
        default=os.environ.get('GATEWAY_DATA', 'data'),
        help='Directory containing .h5ad files (default: $GATEWAY_DATA or data)',
    )
    parser.add_argument(
        '--output',
        default='datasets.tsv',
        help='Output path for datasets.tsv (default: datasets.tsv)',
    )
    parser.add_argument(
        '--merged-file',
        default=None,
        help='Path to merged/meta-analysis .h5ad to append as last row (optional)',
    )
    parser.add_argument(
        '--merged-config',
        default=None,
        help='Path to YAML file with manual field overrides for the merged row (optional)',
    )
    args = parser.parse_args()

    generate_tsv(
        args.data_dir, args.output, args.merged_file, args.merged_config
    )


# Main script entry point
if __name__ == '__main__':
    main()
