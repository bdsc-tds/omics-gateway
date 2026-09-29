"""
Script to write dataset metadata into SpatialData store's table.

Stores then carry same uns keys as team's .h5ad files, so
generate_datasets_tsv.py builds their datasets.tsv rows same way.
convert_xenium.py calls this for --metadata; run it directly to update existing
store without reconverting.

Metadata file is flat YAML mapping of uns keys (UNS_KEYS values in
generate_datasets_tsv.py) to string or list of strings, e.g.:

    dataset_name_short: D1903482ppAD
    dataset_name: Xenium Skin (D1903482)
    article_title: Xenium spatial transcriptomics of atopic dermatitis skin
    assay: Xenium
    disease: Atopic Dermatitis
    tissue: Skin
    patients: 1
    article_date: 2025
    article_authors: A. Thiebaut

cell_count and gene_count can be left out: they default to table size.

Usage:
    conda run -n spatial python data_prep/write_store_metadata.py \
        --zarr data/xenium_D1903482.zarr \
        --metadata data/xenium_D1903482.metadata.yaml
"""

# Import utility modules
import argparse
import os

# Import analysis modules
import numpy as np
import yaml
import zarr
from anndata.io import write_elem

# Import sibling script; data_prep/ is on sys.path when scripts run directly
from generate_datasets_tsv import TABLE_PATH, UNS_KEYS


# Function to read and validate metadata YAML
def load_metadata(metadata_path):
    """
    Read metadata YAML and reject keys generate_datasets_tsv.py would ignore.

    Called before conversion too, so typo fails fast rather than after it.

    Parameters:
    -----------
    metadata_path: str
      Path to YAML file mapping uns keys to values.

    Returns:
    --------
    metadata: dict
      Mapping of uns key to string, or to list of strings.
    """
    with open(metadata_path) as handle:
        metadata = yaml.safe_load(handle) or {}
    unknown = sorted(set(metadata) - set(UNS_KEYS.values()))
    if unknown:
        raise ValueError(
            f'Unknown metadata keys in {metadata_path}: {", ".join(unknown)}. '
            f'Expected: {", ".join(UNS_KEYS.values())}.'
        )

    # Strings throughout, as in team's .h5ad uns; YAML parses 2025 as int
    return {
        key: [str(v) for v in value]
        if isinstance(value, list)
        else ('' if value is None else str(value))
        for key, value in metadata.items()
    }


# Function to write metadata into store's table uns
def write_store_metadata(store_path, metadata):
    """
    Write metadata into uns of store's table and refresh consolidated metadata.

    Only uns elements are written, leaving rest of table untouched: rewriting
    whole table risks undoing conversion fixes such as string dtypes.

    Parameters:
    -----------
    store_path: str
      Path to SpatialData .zarr store.
    metadata: dict
      Mapping returned by load_metadata.

    Returns:
    --------
    keys: list of str
      Keys written, sorted.
    """
    uns = zarr.open_group(
        os.path.join(store_path, TABLE_PATH, 'uns'), mode='r+'
    )
    for key, value in metadata.items():
        if isinstance(value, list):
            value = np.array(value, dtype=object)
        write_elem(uns, key, value)

    # Root index lists every node; stale one hides new keys from SpatialData
    root = zarr.open_group(store_path, mode='r+', use_consolidated=False)
    zarr.consolidate_metadata(root.store)

    return sorted(metadata)


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Write dataset metadata into a SpatialData store.'
    )
    parser.add_argument(
        '--zarr', required=True, help='Path to SpatialData .zarr store.'
    )
    parser.add_argument(
        '--metadata', required=True, help='YAML file of uns keys and values.'
    )

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    if not os.path.isdir(os.path.join(args.zarr, TABLE_PATH)):
        raise FileNotFoundError(f'No table found in store {args.zarr}.')
    keys = write_store_metadata(args.zarr, load_metadata(args.metadata))
    print(f'Wrote {", ".join(keys)} to {args.zarr}')
