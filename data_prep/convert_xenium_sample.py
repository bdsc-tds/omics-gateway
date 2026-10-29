"""
Script to add one Xenium sample to multi-sample SpatialData .zarr store for
Vitessce spatial viewer.

Layout matches convert_visium_hd.py: each sample gets own coordinate system
(named by --key) and elements prefixed by it: '<key>_morphology_focus',
'<key>_cell_boundaries', '<key>_nucleus_boundaries', '<key>_table' and
'<key>_transcripts'; cell ids are prefixed too. Store is created on first
sample and extended afterwards; converting sample again replaces its
elements. 'tables/table' is left free for study-wide merged table
(merge_visium_hd_tables.py).

Only cells of study's joint analysis are kept, read from CSV with 'sample',
'cell_id', 'umap_1', 'umap_2' (joint UMAP, stored as X_umap) and
'sample_umap_1', 'sample_umap_2' (sample's own, X_umap_sample) columns; every
further column becomes cell set list. Sample's UMAP comes from analysis too,
as Xenium Ranger's leaves out some annotated cells. Reading, rotation and
transcript preparation are those of convert_xenium.py, so its memory notes
apply.

Usage (cluster batch job; one sample per call, chained, since parallel
writes race on root metadata):
    python data_prep/convert_xenium_sample.py \
        --xenium-dir .../xenium_skin_1 \
        --sample-id S1 --key s1 \
        --joint joint_umap.csv \
        --out xenium_skin.zarr
"""

# Import utility modules
import argparse
import os
import time

import geopandas as gpd
import pandas as pd

# Import sibling scripts; data_prep/ is on sys.path when scripts run directly
from convert_visium_hd import write_elements
from convert_xenium import (
    MIN_TRANSCRIPT_QV,
    TRANSCRIPT_ROW_GROUP_SIZE,
    add_global_centroids,
    downcast_int64_obs,
    normalise_string_dtypes,
    peak_rss_gb,
    prepare_transcripts,
    read_xenium,
    reindex_nucleus_boundaries,
    retarget_table_region,
    rotate_sample,
)
from spatialdata.models import ShapesModel
from spatialdata.transformations import get_transformation, set_transformation
from vitessce.data_utils import sdata_points_modify_row_group_size

# Joint analysis CSV columns that are not cell sets
JOINT_KEY_COLUMNS = [
    'sample',
    'cell_id',
    'umap_1',
    'umap_2',
    'sample_umap_1',
    'sample_umap_2',
]


# Function to read joint analysis rows of one sample
def read_joint_cells(joint_csv, sample_id):
    """
    Read joint analysis rows of one sample, indexed by Xenium cell id.

    Parameters:
    -----------
    joint_csv: str
      CSV with JOINT_KEY_COLUMNS and cell set columns.
    sample_id: str
      Value of 'sample' column selecting this sample's rows.

    Returns:
    --------
    cells: pandas.DataFrame
      Cell set and UMAP columns, indexed by unprefixed cell id.
    """
    joint = pd.read_csv(joint_csv)
    cells = joint[joint['sample'] == sample_id].set_index('cell_id')
    if cells.empty:
        raise ValueError(f'No cells of {sample_id} in {joint_csv}.')
    if not cells.index.is_unique:
        raise ValueError(f'Duplicate cell ids for {sample_id} in {joint_csv}.')

    return cells.drop(columns='sample')


# Function to keep joint analysis cells of shapes element under sample's names
def rebuild_shapes(shapes, cell_ids, key):
    """
    Keep polygons of given cells, prefix their ids and move them to sample's
    coordinate system.

    Parameters:
    -----------
    shapes: geopandas.GeoDataFrame
      Cell or nucleus boundaries indexed by cell id, on 'global' transform.
    cell_ids: pandas.Index
      Unprefixed cell ids to keep.
    key: str
      Sample key: coordinate system name and id prefix.

    Returns:
    --------
    shapes: geopandas.GeoDataFrame
      Parsed shapes element; nuclei may repeat ids.
    """
    kept = shapes[shapes.index.isin(cell_ids)]
    frame = gpd.GeoDataFrame(
        geometry=kept.geometry.values,
        index=pd.Index(f'{key}_' + kept.index.astype(str), name='cell_id'),
    )

    return ShapesModel.parse(
        frame, transformations={key: get_transformation(shapes)}
    )


# Function to restrict table to joint analysis cells and attach its results
def build_table(sdata, cells, key):
    """
    Keep joint analysis cells of table, adding its sets and both UMAPs.

    Centroids are copied to rendered frame before ids are prefixed, while
    cell boundaries still sit on 'global' transform.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Freshly read Xenium object, already rotated if requested.
    cells: pandas.DataFrame
      Joint analysis rows from read_joint_cells().
    key: str
      Sample key: id prefix and prefix of annotated region.

    Returns:
    --------
    table: anndata.AnnData
      Parsed table annotating '<key>_cell_boundaries'.
    """
    table = sdata.tables['table']
    missing = cells.index.difference(table.obs['cell_id'])
    if len(missing):
        raise ValueError(f'{len(missing)} joint cells absent from bundle.')
    keep = table.obs['cell_id'].isin(cells.index).to_numpy()
    # Re-read, as centroid and region steps below edit store's own table
    sdata.tables['table'] = table[keep].copy()
    table = sdata.tables['table']
    ids = table.obs['cell_id'].to_numpy()

    set_cols = [c for c in cells.columns if c not in JOINT_KEY_COLUMNS]
    for column in set_cols:
        values = cells[column].reindex(ids).fillna('Unassigned').astype(str)
        table.obs[column] = pd.Categorical(values.to_numpy())
    for obsm_key, stem in (
        ('X_umap', 'umap'),
        ('X_umap_sample', 'sample_umap'),
    ):
        table.obsm[obsm_key] = (
            cells[[f'{stem}_1', f'{stem}_2']].reindex(ids).to_numpy('float32')
        )

    add_global_centroids(sdata, 'cell_boundaries')

    table.obs_names = pd.Index(f'{key}_' + ids.astype(str))
    table.obs['cell_id'] = table.obs_names.to_numpy()
    # CSC: viewer reads one gene's slice; CSR makes it densify whole matrix
    table.X = table.X.astype('float32').tocsc()
    retarget_table_region(sdata, f'{key}_cell_boundaries')

    # Must run last, after every column steps above may add
    normalise_string_dtypes(table)
    downcast_int64_obs(table)

    return table


# Function to move element to sample's coordinate system
def move_to_coordinate_system(element, key):
    """
    Replace element's 'global' transform by same transform under sample key.

    Parameters:
    -----------
    element: spatialdata element
      Image or points element on 'global' transform.
    key: str
      Sample key, used as coordinate system name.

    Returns:
    --------
    None
    """
    set_transformation(
        element, {key: get_transformation(element)}, set_all=True
    )


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments for Xenium sample conversion.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Add a Xenium sample to a SpatialData .zarr store.'
    )
    parser.add_argument(
        '--xenium-dir', required=True, help='Xenium Ranger output directory.'
    )
    parser.add_argument(
        '--sample-id', required=True, help='Sample id in joint analysis CSV.'
    )
    parser.add_argument(
        '--key', required=True, help='Element prefix and coordinate system.'
    )
    parser.add_argument(
        '--joint', required=True, help='Joint analysis CSV (cells, UMAP, sets).'
    )
    parser.add_argument('--out', required=True, help='Destination .zarr store.')
    parser.add_argument(
        '--rotate',
        type=int,
        default=0,
        choices=[0, 90, 180, 270],
        help='Rotate the sample clockwise by this many degrees.',
    )

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    if not os.path.isdir(args.xenium_dir):
        raise FileNotFoundError(
            f'Xenium directory {os.path.abspath(args.xenium_dir)} does not exist.'
        )
    start = time.time()
    prefix = f'{args.key}_'

    cells = read_joint_cells(args.joint, args.sample_id)
    print(f'{len(cells)} joint analysis cells', flush=True)
    sdata = read_xenium(
        args.xenium_dir,
        transcripts=True,
        cells_labels=False,
        nucleus_boundaries=True,
    )
    if args.rotate:
        size = rotate_sample(sdata, args.rotate)
        print(
            f'rotated {args.rotate} degrees clockwise ({size[0]}x{size[1]} px)',
            flush=True,
        )
    n = reindex_nucleus_boundaries(sdata)
    print(f'nuclei reindexed by cell id ({n} duplicate ids)', flush=True)

    table = build_table(sdata, cells, args.key)
    cell_shapes = rebuild_shapes(
        sdata.shapes['cell_boundaries'], cells.index, args.key
    )
    nuclei = rebuild_shapes(
        sdata.shapes['nucleus_boundaries'], cells.index, args.key
    )
    if len(cell_shapes) != table.n_obs:
        raise ValueError(
            f'{len(cell_shapes)} cell polygons for {table.n_obs} cells.'
        )
    print(
        f'table {table.n_obs} x {table.n_vars}; {len(cell_shapes)} cell and '
        f'{len(nuclei)} nucleus polygons',
        flush=True,
    )
    image = sdata.images['morphology_focus']
    move_to_coordinate_system(image, args.key)

    # Transcripts held back so their sort does not run alongside image pyramid
    write_elements(
        args.out,
        {
            f'{prefix}cell_boundaries': cell_shapes,
            f'{prefix}nucleus_boundaries': nuclei,
            f'{prefix}table': table,
            f'{prefix}morphology_focus': image,
        },
    )
    step = time.time()
    n, n_low_qv = prepare_transcripts(sdata, table.var.index)
    print(
        f'transcripts prepared in {time.time() - step:.1f}s ({n} control-probe '
        f'detections and {n_low_qv} below qv {MIN_TRANSCRIPT_QV} dropped)',
        flush=True,
    )
    points = sdata.points['transcripts']
    move_to_coordinate_system(points, args.key)
    backed = write_elements(args.out, {f'{prefix}transcripts': points})
    sdata_points_modify_row_group_size(
        backed, f'{prefix}transcripts', TRANSCRIPT_ROW_GROUP_SIZE
    )
    backed.write_consolidated_metadata()
    print(
        f'done in {time.time() - start:.1f}s, peak RSS {peak_rss_gb():.2f} GB '
        f'-> {args.out}',
        flush=True,
    )
