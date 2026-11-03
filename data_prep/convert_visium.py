"""
Script to add one classic Visium sample to multi-sample SpatialData .zarr
store for Vitessce spatial viewer.

Layout matches convert_visium_hd.py: each sample gets own coordinate system
(named by --key) and elements prefixed by it: '<key>_he' (H&E), '<key>_spots'
(circles) and '<key>_table'; spot ids are prefixed too. Store is created on
first sample and extended afterwards; converting sample again replaces its
elements. 'tables/table' is left free for study-wide merged table
(merge_study_tables.py).

Input is scanpy-style .h5ad (raw counts in X, spot centres in full-resolution
pixels in obsm['spatial'], images and scale factors in uns['spatial']), as
written by scanpy.read_visium(). Global frame of sample is its hires image's
pixels, so spots are stored in that frame, with no transform to resolve.
Space Ranger's spot_diameter_fullres does not match spots' 55 um, so
microns per pixel comes from spot centre spacing, 100 um on every Visium
slide.

Only spots of study's joint analysis are kept, read from CSV in
convert_xenium_sample.py's format; every further column becomes set list.

Usage (one sample per call, chained, since parallel writes race on root
metadata):
    python data_prep/convert_visium.py \
        --h5ad s1.h5ad --sample-id S1 --key s1 \
        --joint joint_analysis.csv \
        --out visium_study.zarr
"""

# Import utility modules
import argparse
import os
import time

import anndata as ad
import geopandas as gpd
import numpy as np
import pandas as pd
import zarr

# Import sibling scripts; data_prep/ is on sys.path when scripts run directly
from convert_visium_hd import (
    CENTROIDS_KEY,
    TILE,
    set_rgb_channel_rendering,
    write_elements,
)
from convert_xenium import (
    add_genes_detected,
    downcast_int64_obs,
    normalise_string_dtypes,
    peak_rss_gb,
)
from convert_xenium_sample import JOINT_KEY_COLUMNS, read_joint_cells
from scipy.spatial import cKDTree
from shapely.geometry import Point
from spatialdata.models import Image2DModel, ShapesModel, TableModel
from spatialdata.transformations import Identity

# Visium slide geometry, identical on every capture area
SPOT_PITCH_UM = 100
SPOT_DIAMETER_UM = 55

# Region suffix; config generator's SPOTS_SUFFIX must match
SPOTS_SUFFIX = '_spots'


# Function to measure full-resolution pixel size from spot spacing
def microns_per_fullres_pixel(spot_xy):
    """
    Derive microns per full-resolution pixel from spot centre spacing.

    Parameters:
    -----------
    spot_xy: numpy.ndarray
      (n, 2) spot centres in full-resolution pixels.

    Returns:
    --------
    microns: float
      Microns per full-resolution pixel.
    """
    distances, _ = cKDTree(spot_xy).query(spot_xy, k=2)

    return SPOT_PITCH_UM / float(np.median(distances[:, 1]))


# Function to wrap hires H&E as image element
def build_he_image(hires, key):
    """
    Convert scanpy's hires image to 8-bit RGB image element.

    Parameters:
    -----------
    hires: numpy.ndarray
      (y, x, 3) image, floats in 0-1 or 8-bit integers.
    key: str
      Coordinate system name.

    Returns:
    --------
    element: xarray.DataArray
      Single-scale image on identity transform.
    """
    if hires.ndim != 3 or hires.shape[2] != 3:
        raise ValueError(f'Expected (y, x, 3) RGB image, got {hires.shape}.')
    if hires.dtype != np.uint8:
        hires = np.round(np.clip(hires, 0, 1) * 255).astype('uint8')

    return Image2DModel.parse(
        hires.transpose(2, 0, 1),
        dims=('c', 'y', 'x'),
        c_coords=['R', 'G', 'B'],
        chunks=(1, TILE, TILE),
        transformations={key: Identity()},
    )


# Function to build circles element of kept spots
def build_spots(spot_xy, spot_ids, radius, key):
    """
    Build circles element of spots in sample's global frame.

    Parameters:
    -----------
    spot_xy: numpy.ndarray
      (n, 2) spot centres in sample's global frame.
    spot_ids: numpy.ndarray
      Prefixed spot ids, in same order.
    radius: float
      Spot radius in global frame's units.
    key: str
      Coordinate system name.

    Returns:
    --------
    shapes: geopandas.GeoDataFrame
      Parsed shapes element indexed by spot id.
    """
    frame = gpd.GeoDataFrame(
        {'radius': np.full(len(spot_ids), radius)},
        geometry=[Point(x, y) for x, y in spot_xy],
        index=pd.Index(spot_ids, name='cell_id'),
    )

    return ShapesModel.parse(frame, transformations={key: Identity()})


# Function to build table of kept spots
def build_table(adata, spots, prefix, region, frame_xy):
    """
    Build AnnData of kept spots with counts, QC metrics, sets and UMAPs.

    Parameters:
    -----------
    adata: anndata.AnnData
      Sample's spots, raw counts in X.
    spots: pandas.DataFrame
      Joint analysis rows from read_joint_cells(), indexed by barcode.
    prefix: str
      Prefix of spot ids in store.
    region: str
      Name of spots element table annotates.
    frame_xy: numpy.ndarray
      (n, 2) centres of kept spots in sample's global frame.

    Returns:
    --------
    table: anndata.AnnData
      Parsed table element.
    """
    counts = adata[spots.index].copy()
    # CSC: viewer reads one gene's slice; CSR makes it densify whole matrix
    counts.X = counts.X.astype('float32').tocsc()

    obs = pd.DataFrame(index=pd.Index(prefix + spots.index.values))
    obs['cell_id'] = obs.index.values
    obs['region'] = pd.Categorical([region] * len(obs))
    # Per-spot QC measurement, offered as viewer metric
    obs['umi_counts'] = np.asarray(counts.X.sum(axis=1)).ravel().astype('int32')
    set_cols = [c for c in spots.columns if c not in JOINT_KEY_COLUMNS]
    for column in set_cols:
        values = spots[column].fillna('Unassigned').astype(str)
        obs[column] = pd.Categorical(values.to_numpy())

    var_cols = [c for c in ('gene_ids', 'feature_types') if c in counts.var]
    table = ad.AnnData(X=counts.X, obs=obs, var=counts.var[var_cols].copy())
    add_genes_detected(table)
    table.obsm['X_umap'] = spots[['umap_1', 'umap_2']].to_numpy('float32')
    table.obsm['X_umap_sample'] = spots[
        ['sample_umap_1', 'sample_umap_2']
    ].to_numpy('float32')
    table.obsm['spatial'] = np.asarray(counts.obsm['spatial'], dtype='float64')
    table.obsm[CENTROIDS_KEY] = frame_xy

    normalise_string_dtypes(table)
    downcast_int64_obs(table)

    return TableModel.parse(
        table, region=region, region_key='region', instance_key='cell_id'
    )


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments for classic Visium sample conversion.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Add a classic Visium sample to a SpatialData .zarr store.'
    )
    parser.add_argument(
        '--h5ad', required=True, help='Scanpy-style Visium .h5ad file.'
    )
    parser.add_argument(
        '--sample-id', required=True, help='Sample id in joint analysis CSV.'
    )
    parser.add_argument(
        '--key', required=True, help='Element prefix and coordinate system.'
    )
    parser.add_argument(
        '--joint', required=True, help='Joint analysis CSV (spots, UMAP, sets).'
    )
    parser.add_argument('--out', required=True, help='Destination .zarr store.')

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    start = time.time()
    prefix = f'{args.key}_'
    region = f'{args.key}{SPOTS_SUFFIX}'

    spots = read_joint_cells(args.joint, args.sample_id)
    adata = ad.read_h5ad(args.h5ad)
    adata.var_names_make_unique()
    missing = spots.index.difference(adata.obs_names)
    if len(missing):
        raise ValueError(f'{len(missing)} joint spots absent from {args.h5ad}.')
    libraries = list(adata.uns['spatial'])
    if len(libraries) != 1:
        raise ValueError(f'Expected one library in uns, got {libraries}.')
    library = adata.uns['spatial'][libraries[0]]
    scale = library['scalefactors']['tissue_hires_scalef']

    microns = microns_per_fullres_pixel(np.asarray(adata.obsm['spatial']))
    kept_xy = np.asarray(adata[spots.index].obsm['spatial'], dtype='float64')
    frame_xy = kept_xy * scale
    radius = SPOT_DIAMETER_UM / 2 / microns * scale
    print(
        f'{len(spots)} joint analysis spots; {microns:.3f} um per full-res '
        f'pixel, hires scale {scale:.4f}, radius {radius:.2f} px',
        flush=True,
    )

    image = build_he_image(library['images']['hires'], args.key)
    shapes = build_spots(
        frame_xy, prefix + spots.index.values, radius, args.key
    )
    table = build_table(adata, spots, prefix, region, frame_xy)
    print(f'table {table.n_obs} x {table.n_vars}', flush=True)

    backed = write_elements(
        args.out,
        {region: shapes, f'{prefix}table': table, f'{prefix}he': image},
    )
    set_rgb_channel_rendering(args.out, f'{prefix}he')
    # Scale bar reads it; hires pixel is full-res pixel divided by scale
    group = zarr.open_group(
        os.path.join(args.out, 'images', f'{prefix}he'), mode='r+'
    )
    group.attrs.update(microns_per_pixel=microns / scale)
    backed.write_consolidated_metadata()
    print(
        f'done in {time.time() - start:.1f}s, peak RSS {peak_rss_gb():.2f} GB '
        f'-> {args.out}',
        flush=True,
    )
