"""
Script to add one Visium HD sample to multi-sample SpatialData .zarr store
for Vitessce spatial viewer.

Each sample gets own coordinate system (named by --key) and elements
prefixed by it: '<key>_he' (H&E pyramid), '<key>_cell_boundaries' and
'<key>_nucleus_boundaries' (Space Ranger segmentation), '<key>_table'. Store
is created on first sample and extended afterwards, element by element;
converting sample again replaces its elements.
'tables/table' is left free for study-wide merged table.

Only cells passing analysts' QC are kept: those in merged Harmony object,
exported as CSV (cell, sample, cluster, umap_1, umap_2, cell_id) by
extract_merged.R. Table X holds Space Ranger raw counts, as Xenium stores do.

H&E is read tile by tile and written as RGB pyramid starting at
1/--downsample of scan resolution; global frame of sample is that level's
pixels, so Space Ranger geometry (full-resolution pixels) gets Scale.

Usage (cluster batch job; one sample per call):
    python data_prep/convert_visium_hd.py \
        --outs .../space_ranger/PSO1/1/A1/A1_PSO1/outs \
        --he .../he_images/D2002110.ome.tif \
        --sample-id A1_PSO1 --key pso1 \
        --rctd .../analysis/rctd_cell/pso1_rctd_der.csv \
        --merged-umap merged_umap_srcells.csv \
        --out visium_hd_skin.zarr
"""

# Import utility modules
import argparse
import json
import math
import os
import shutil
import time

import anndata as ad
import dask
import dask.array as da
import geopandas as gpd
import numpy as np
import pandas as pd
import scanpy as sc
import tifffile
import zarr

# Import sibling script; data_prep/ is on sys.path when scripts run directly
from convert_xenium import (
    downcast_int64_obs,
    normalise_string_dtypes,
    peak_rss_gb,
)
from spatialdata import SpatialData, read_zarr
from spatialdata.models import Image2DModel, ShapesModel, TableModel
from spatialdata.transformations import Identity, Scale

# Tile edge in viewer; also chunk size of every image level
TILE = 2048

# RCTD classes whose first_type is not confident
REJECT_CLASSES = {'reject'}

# Centroids pre-scaled to rendered coordinates, read by config generator
CENTROIDS_KEY = 'spatial_global'


# Function to read analysts' QC-passed cells of one sample
def read_qc_cells(merged_csv, sample_id):
    """
    Read merged-object rows of one sample, indexed by Space Ranger cell id.

    Parameters:
    -----------
    merged_csv: str
      CSV exported by extract_merged.R.
    sample_id: str
      Space Ranger sample id as in merged object's orig.ident (e.g. A1_PSO1).

    Returns:
    --------
    cells: pandas.DataFrame
      Columns cluster, umap_1, umap_2, indexed by cell id ('cellid_...-1').
    """
    merged = pd.read_csv(merged_csv)
    cells = merged[merged['sample'] == sample_id].set_index('cell_id')
    if cells.empty:
        raise ValueError(f'No cells of {sample_id} in {merged_csv}.')
    if not cells.index.is_unique:
        raise ValueError(f'Duplicate cell ids for {sample_id} in {merged_csv}.')

    return cells[['cluster', 'umap_1', 'umap_2']]


# Function to read Space Ranger polygons of kept cells
def read_polygons(geojson_path, cell_ids, key, scale):
    """
    Read Space Ranger cell or nucleus polygons, keeping only given cells.

    Parameters:
    -----------
    geojson_path: str
      Space Ranger segmentation GeoJSON (full-resolution pixel coordinates).
    cell_ids: pandas.Index
      Cell ids to keep ('cellid_...-1').
    key: str
      Sample key: coordinate system name, and prefix making ids unique
      across samples of store.
    scale: float
      Factor from full-resolution pixels to sample's global frame.

    Returns:
    --------
    shapes: geopandas.GeoDataFrame
      Parsed shapes element indexed by prefixed cell id; nuclei may repeat ids.
    """
    frame = gpd.read_file(geojson_path)
    ids = frame['cell_id'].map(lambda n: f'cellid_{int(n):09d}-1')
    frame = frame[ids.isin(cell_ids)]
    ids = ids[frame.index]
    frame = gpd.GeoDataFrame(
        geometry=frame.geometry.values,
        index=pd.Index(f'{key}_' + ids.values, name='cell_id'),
    )

    return ShapesModel.parse(
        frame, transformations={key: Scale([scale, scale], axes=('x', 'y'))}
    )


# Function to build table of kept cells
def build_table(outs, cells, rctd_csv, prefix, region, cell_shapes, scale):
    """
    Build AnnData of kept cells with counts, cell types, clusters and UMAPs.

    Parameters:
    -----------
    outs: str
      Space Ranger outs directory.
    cells: pandas.DataFrame
      QC-passed cells from read_qc_cells().
    rctd_csv: str
      RCTD result with 'Barcode', 'spot_class' and 'first_type' columns.
    prefix: str
      Prefix of cell ids in store.
    region: str
      Name of cell shapes element table annotates.
    cell_shapes: geopandas.GeoDataFrame
      Kept cell polygons, used for centroids.
    scale: float
      Factor from full-resolution pixels to sample's global frame.

    Returns:
    --------
    table: anndata.AnnData
      Parsed table element.
    """
    counts = sc.read_10x_h5(
        os.path.join(
            outs, 'segmented_outputs', 'filtered_feature_cell_matrix.h5'
        )
    )
    counts.var_names_make_unique()
    missing = cells.index.difference(counts.obs_names)
    if len(missing):
        raise ValueError(
            f'{len(missing)} QC cells absent from Space Ranger matrix.'
        )
    counts = counts[cells.index].copy()
    counts.X = counts.X.astype('float32')

    obs = pd.DataFrame(index=pd.Index(prefix + cells.index.values))
    obs['cell_id'] = obs.index.values
    obs['region'] = pd.Categorical([region] * len(obs))

    # Confident list blanks reject class; best guess keeps every first_type
    rctd = pd.read_csv(rctd_csv).set_index('Barcode')
    first = rctd['first_type'].reindex(cells.index)
    confident = first.where(
        ~rctd['spot_class'].reindex(cells.index).isin(REJECT_CLASSES)
    )
    obs['cell_type'] = pd.Categorical(confident.fillna('Unassigned').values)
    obs['cell_type_best_guess'] = pd.Categorical(
        first.fillna('Unassigned').values
    )
    # String categories: int64 column would blank viewer metrics
    obs['cluster'] = pd.Categorical(cells['cluster'].astype(str).values)

    table = ad.AnnData(X=counts.X, obs=obs, var=counts.var)
    table.obsm['X_umap'] = cells[['umap_1', 'umap_2']].to_numpy('float32')

    projection = os.path.join(
        outs,
        'segmented_outputs',
        'analysis',
        'umap',
        'gene_expression_2_components',
        'projection.csv',
    )
    umap = pd.read_csv(projection).set_index('Barcode').reindex(cells.index)
    if umap.isna().any().any():
        raise ValueError('QC cells missing from Space Ranger UMAP.')
    table.obsm['X_umap_sample'] = umap.to_numpy('float32')

    centroids = cell_shapes.geometry.centroid.reindex(table.obs_names)
    full_res = np.column_stack([centroids.x, centroids.y]).astype('float64')
    table.obsm['spatial'] = full_res
    table.obsm[CENTROIDS_KEY] = full_res * scale

    normalise_string_dtypes(table)
    downcast_int64_obs(table)

    return TableModel.parse(
        table, region=region, region_key='region', instance_key='cell_id'
    )


# Function to build RGB pyramid of H&E scan
def build_he_pyramid(he_path, downsample, key):
    """
    Read H&E OME-TIFF lazily and wrap it as multiscale RGB image element.

    Parameters:
    -----------
    he_path: str
      OME-TIFF used by Space Ranger as tissue image.
    downsample: int
      Integer factor applied before level s0.
    key: str
      Coordinate system name.

    Returns:
    --------
    element: xarray.DataTree
      Multiscale image on identity transform.
    """
    store = tifffile.imread(he_path, aszarr=True, level=0)
    image = da.from_zarr(zarr.open(store, mode='r'))
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f'Expected (y, x, 3) RGB image, got {image.shape}.')
    # TIFF tiles are 256 px; larger blocks keep task graph small
    image = image.rechunk((TILE * 2, TILE * 2, 3))
    if downsample > 1:
        image = da.coarsen(
            np.mean, image, {0: downsample, 1: downsample}, trim_excess=True
        )
        image = da.round(image).astype('uint8')
    image = image.transpose(2, 0, 1).rechunk((1, TILE, TILE))

    # Halve until longest side fits one tile
    n_extra = max(0, math.ceil(math.log2(max(image.shape[1:]) / TILE)))

    return Image2DModel.parse(
        image,
        dims=('c', 'y', 'x'),
        c_coords=['R', 'G', 'B'],
        scale_factors=[2] * n_extra,
        chunks=(1, TILE, TILE),
        transformations={key: Identity()},
    )


# Function to delete elements written by earlier run
def remove_existing_elements(out_path, names):
    """
    Delete named elements from store on disk and consolidate metadata again.

    SpatialData can neither overwrite nor delete element loaded from same
    store, so sample being converted again is removed before store is read.

    Parameters:
    -----------
    out_path: str
      Existing .zarr store.
    names: iterable of str
      Element names to remove wherever found.
    """
    removed = []
    for group in ('images', 'labels', 'points', 'shapes', 'tables'):
        for name in names:
            path = os.path.join(out_path, group, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(f'{group}/{name}')
    if removed:
        # Root metadata still lists removed elements, which read_zarr opens
        zarr.consolidate_metadata(out_path)
        print(f'  replacing {", ".join(removed)}', flush=True)


# Function to write elements into new or existing store
def write_elements(out_path, elements):
    """
    Write elements one by one into store, creating store if needed.

    Elements already in store, from earlier run of same sample, are replaced.

    Parameters:
    -----------
    out_path: str
      Destination .zarr store.
    elements: dict
      Element name to element, written in insertion order.

    Returns:
    --------
    backed: spatialdata.SpatialData
      Store-backed object.
    """
    if os.path.isdir(out_path):
        remove_existing_elements(out_path, elements)
        backed = read_zarr(out_path)
    else:
        backed = SpatialData()
        backed.write(out_path)
    for name, element in elements.items():
        step = time.time()
        backed[name] = element
        backed.write_element(name, overwrite=True)
        print(
            f'  wrote {name} in {time.time() - step:.1f}s '
            f'(peak RSS {peak_rss_gb():.2f} GB)',
            flush=True,
        )

    return backed


# Function to make RGB image render as true-colour channels
def set_rgb_channel_rendering(out_path, image_name):
    """
    Give image's R, G, B channels pure colours and full 0-255 windows.

    Viewer initialises each channel's colour and contrast from these OME
    'omero' fields; without them it uses its fluorescence palette and H&E
    turns violet. Rendered additively, they reproduce scan's colours, while
    each channel keeps own row, toggle and contrast slider.

    Parameters:
    -----------
    out_path: str
      SpatialData .zarr store.
    image_name: str
      Image element name.

    Returns:
    --------
    None
    """
    group = zarr.open_group(
        os.path.join(out_path, 'images', image_name), mode='r+'
    )
    ome = dict(group.attrs['ome'])
    for channel, label, colour in zip(
        ome['omero']['channels'],
        ('R', 'G', 'B'),
        ('FF0000', '00FF00', '0000FF'),
        strict=True,
    ):
        # Label set too, so stores written with lowercase labels match
        channel['label'] = label
        channel['color'] = colour
        channel['window'] = {'start': 0, 'end': 255, 'min': 0, 'max': 255}
    # Layer row's name in viewer, 'Image' otherwise
    ome['omero']['name'] = 'H&E'
    group.attrs.update(ome=ome)


# Function to record image's pixel size for viewer's scale bar
def set_microns_per_pixel(out_path, image_name, outs, downsample):
    """
    Store microns per pixel of image's level s0 as image attribute.

    Config generator reads it for scale bar; Space Ranger shapes are in
    pixels, not microns, so it cannot be derived from them as for Xenium.

    Parameters:
    -----------
    out_path: str
      SpatialData .zarr store.
    image_name: str
      Image element name.
    outs: str
      Space Ranger outs directory, whose scale factors give scan's size.
    downsample: int
      Factor applied to scan before level s0.

    Returns:
    --------
    microns: float
      Microns per pixel written.
    """
    factors_path = os.path.join(
        outs, 'segmented_outputs', 'spatial', 'scalefactors_json.json'
    )
    with open(factors_path) as handle:
        microns = json.load(handle)['microns_per_pixel'] * downsample
    group = zarr.open_group(
        os.path.join(out_path, 'images', image_name), mode='r+'
    )
    group.attrs.update(microns_per_pixel=microns)

    return microns


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments for Visium HD sample conversion.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Add a Visium HD sample to a SpatialData .zarr store.'
    )
    parser.add_argument('--outs', required=True, help='Space Ranger outs dir.')
    parser.add_argument('--he', required=True, help='H&E OME-TIFF path.')
    parser.add_argument(
        '--sample-id', required=True, help='Sample id in merged object.'
    )
    parser.add_argument(
        '--key', required=True, help='Element prefix and coordinate system.'
    )
    parser.add_argument('--rctd', required=True, help='RCTD result CSV.')
    parser.add_argument(
        '--merged-umap', required=True, help='CSV from extract_merged.R.'
    )
    parser.add_argument('--out', required=True, help='Destination .zarr store.')
    parser.add_argument('--downsample', type=int, default=2)
    parser.add_argument('--workers', type=int, default=4)

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    dask.config.set(scheduler='threads', num_workers=args.workers)
    start = time.time()
    prefix = f'{args.key}_'
    scale = 1 / args.downsample
    region = f'{prefix}cell_boundaries'
    segmented = os.path.join(args.outs, 'segmented_outputs')

    cells = read_qc_cells(args.merged_umap, args.sample_id)
    print(f'{len(cells)} QC-passed cells', flush=True)
    cell_shapes = read_polygons(
        os.path.join(segmented, 'cell_segmentations.geojson'),
        cells.index,
        args.key,
        scale,
    )
    nuclei = read_polygons(
        os.path.join(segmented, 'nucleus_segmentations.geojson'),
        cells.index,
        args.key,
        scale,
    )
    if len(cell_shapes) != len(cells) or not cell_shapes.index.is_unique:
        raise ValueError(
            f'{len(cell_shapes)} cell polygons for {len(cells)} QC cells.'
        )
    print(
        f'{len(cell_shapes)} cell and {len(nuclei)} nucleus polygons',
        flush=True,
    )

    table = build_table(
        args.outs, cells, args.rctd, prefix, region, cell_shapes, scale
    )
    print(
        f'table {table.n_obs} x {table.n_vars}; Unassigned: '
        f'{int((table.obs["cell_type"] == "Unassigned").sum())} confident, '
        f'{int((table.obs["cell_type_best_guess"] == "Unassigned").sum())} '
        'best guess',
        flush=True,
    )
    image = build_he_pyramid(args.he, args.downsample, args.key)

    backed = write_elements(
        args.out,
        {
            region: cell_shapes,
            f'{prefix}nucleus_boundaries': nuclei,
            f'{prefix}table': table,
            f'{prefix}he': image,
        },
    )
    set_rgb_channel_rendering(args.out, f'{prefix}he')
    set_microns_per_pixel(args.out, f'{prefix}he', args.outs, args.downsample)
    backed.write_consolidated_metadata()
    print(
        f'done in {time.time() - start:.1f}s, peak RSS {peak_rss_gb():.2f} GB '
        f'-> {args.out}',
        flush=True,
    )
