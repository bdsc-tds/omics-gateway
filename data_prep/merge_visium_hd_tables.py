"""
Script to add study-wide merged table to multi-sample Visium HD store.

Concatenates per-sample tables written by convert_visium_hd.py into
'tables/table', which annotates every sample's cell boundaries and carries
study metadata read by generate_datasets_tsv.py. Adds 'sample' and 'disease'
columns. Keeps only joint analysis UMAP (X_umap): per-sample UMAPs and
spatial coordinates each live in own sample's frame. Also writes
'tables/<key>_umap_all_cells' per sample: every cell's joint UMAP, for
sample's Merged UMAP view.

Run again after any sample is converted again: existing tables are
replaced.

Usage (cluster batch job, once every sample is converted):
    python data_prep/merge_visium_hd_tables.py \
        --zarr visium_hd_skin.zarr \
        --sample pso1 PSO1 Psoriasis \
        --sample lp1 LP1 'Lichen Planus' \
        --metadata visium_hd_skin.metadata.yaml
"""

# Import utility modules
import argparse
import time

import anndata as ad
import numpy as np
import pandas as pd

# Import sibling scripts; data_prep/ is on sys.path when scripts run directly
from convert_visium_hd import remove_existing_elements
from convert_xenium import (
    downcast_int64_obs,
    normalise_string_dtypes,
    peak_rss_gb,
)
from spatialdata import read_zarr
from spatialdata.models import TableModel
from write_store_metadata import load_metadata, write_store_metadata

# Merged table element, where generate_datasets_tsv.py looks for metadata
MERGED_TABLE = 'table'

# obs columns shown as cell sets; concat turns differing categories to object
CATEGORICAL_COLUMNS = ['cell_type', 'cell_type_best_guess', 'cluster']

# Per-sample table of all cells' merged UMAP, read by config generator
ALL_CELLS_UMAP_SUFFIX = '_umap_all_cells'


# Function to concatenate per-sample tables into one
def merge_tables(sdata, samples):
    """
    Concatenate samples' tables, adding sample and disease columns.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Store holding '<key>_table' of every sample.
    samples: list of tuple
      (key, sample label, disease) per sample, in display order.

    Returns:
    --------
    merged: anndata.AnnData
      Parsed table annotating every sample's cell boundaries.
    """
    tables = [sdata.tables[f'{key}_table'] for key, _, _ in samples]
    genes = tables[0].var_names
    for (key, _, _), table in zip(samples, tables):
        # Inner join would drop genes silently
        if not table.var_names.equals(genes):
            raise ValueError(f'Genes of {key}_table differ from first table.')

    merged = ad.concat(tables, merge='same')
    if not merged.obs_names.is_unique:
        raise ValueError('Cell ids repeat across samples.')
    # Concat returns CSR even from CSC tables
    merged.X = merged.X.tocsc()
    merged.obsm = {'X_umap': merged.obsm['X_umap']}

    labels = [label for _, label, _ in samples]
    sizes = [table.n_obs for table in tables]
    merged.obs['sample'] = pd.Categorical(
        [label for label, n in zip(labels, sizes) for _ in range(n)],
        categories=labels,
    )
    diseases = [disease for _, _, disease in samples]
    merged.obs['disease'] = pd.Categorical([
        disease for disease, n in zip(diseases, sizes) for _ in range(n)
    ])
    for column in CATEGORICAL_COLUMNS:
        merged.obs[column] = pd.Categorical(merged.obs[column].astype(str))
    regions = [table.obs['region'].iloc[0] for table in tables]
    merged.obs['region'] = pd.Categorical(
        merged.obs['region'].astype(str), categories=regions
    )

    normalise_string_dtypes(merged)
    downcast_int64_obs(merged)

    return TableModel.parse(
        merged, region=regions, region_key='region', instance_key='cell_id'
    )


# Function to build one sample's all-cells UMAP table
def all_cells_umap(merged, key):
    """
    Build table of every cell's joint analysis UMAP, sample's cells last.

    Sample's config draws Merged UMAP from it, so other samples' cells show
    as background; points draw in row order, so sample's come last, on top.

    Parameters:
    -----------
    merged: anndata.AnnData
      Table built by merge_tables().
    key: str
      Sample key in store.

    Returns:
    --------
    table: anndata.AnnData
      Parsed table with obs index and X_umap only, annotating no region.
    """
    own = (merged.obs['region'] == f'{key}_cell_boundaries').to_numpy()
    order = list(np.flatnonzero(~own)) + list(np.flatnonzero(own))
    table = ad.AnnData(
        obs=pd.DataFrame(index=pd.Index(merged.obs_names[order], dtype=object)),
        obsm={'X_umap': merged.obsm['X_umap'][order]},
    )
    normalise_string_dtypes(table)

    return TableModel.parse(table)


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments for merged table creation.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Add merged all-samples table to a Visium HD store.'
    )
    parser.add_argument('--zarr', required=True, help='Multi-sample store.')
    parser.add_argument(
        '--sample',
        nargs=3,
        action='append',
        required=True,
        metavar=('KEY', 'LABEL', 'DISEASE'),
        help='Sample key in store, display label and disease; repeat per '
        'sample, in display order.',
    )
    parser.add_argument(
        '--metadata', default=None, help='YAML file of study uns keys.'
    )

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    start = time.time()
    # Read first, so typo fails before store changes
    metadata = load_metadata(args.metadata) if args.metadata else None

    umap_tables = [f'{key}{ALL_CELLS_UMAP_SUFFIX}' for key, _, _ in args.sample]
    remove_existing_elements(args.zarr, [MERGED_TABLE, *umap_tables])
    sdata = read_zarr(args.zarr)
    merged = merge_tables(sdata, args.sample)
    print(
        f'merged table {merged.n_obs} x {merged.n_vars}; obs dtypes: '
        f'{dict(merged.obs.dtypes.astype(str))}',
        flush=True,
    )
    print(merged.obs['sample'].value_counts(sort=False).to_dict(), flush=True)

    sdata[MERGED_TABLE] = merged
    sdata.write_element(MERGED_TABLE)
    for (key, _, _), name in zip(args.sample, umap_tables):
        sdata[name] = all_cells_umap(merged, key)
        sdata.write_element(name)
    sdata.write_consolidated_metadata()
    if metadata:
        keys = write_store_metadata(args.zarr, metadata)
        print(f'wrote metadata {", ".join(keys)}', flush=True)
    print(
        f'done in {time.time() - start:.1f}s, peak RSS {peak_rss_gb():.2f} GB '
        f'-> {args.zarr}',
        flush=True,
    )
