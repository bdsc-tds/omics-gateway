"""
Module of helpers shared by spatial conversion scripts and config generator.

Holds store naming every script must agree on, table fixes viewer needs,
element writing into new or existing stores, joint analysis export reading
and pixel size writing. Library only: run converters, not this module.
"""

# Import utility modules
import os
import resource
import shutil
import time

# Import analysis modules
import numpy as np
import pandas as pd
import zarr
from spatialdata import SpatialData, read_zarr

# Tile edge in viewer; also chunk size of every image level
TILE = 2048

# Centroids pre-scaled to rendered coordinates, read by config generator
CENTROIDS_KEY = 'spatial_global'

# Name suffixes of element sample's table annotates: segmented cells, or
# classic Visium spots (convert_visium.py), which get spot layer instead
CELLS_SUFFIX = '_cell_boundaries'
SPOTS_SUFFIX = '_spots'

# Table of every sample's cells holding joint UMAP, per sample of
# multi-sample store (written by merge_study_tables.py)
ALL_CELLS_UMAP_SUFFIX = '_umap_all_cells'

# Joint analysis CSV columns that are not cell sets
JOINT_KEY_COLUMNS = [
    'sample',
    'cell_id',
    'umap_1',
    'umap_2',
    'sample_umap_1',
    'sample_umap_2',
]


# Function to report peak resident memory of process
def peak_rss_gb():
    """
    Report peak resident set size of current process.

    Returns:
    --------
    peak: float
      Maximum resident set size reached so far, in gibibytes.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


# Function to count genes detected per cell
def add_genes_detected(table):
    """
    Store number of genes with non-zero count per cell as obs column.

    Viewer offers it as metric beside counts. 10x matrices hold no explicit
    zeros, so stored entries per row are genes detected.

    Parameters:
    -----------
    table: anndata.AnnData
      Table with sparse counts in X; obs gains 'genes_detected' in place.

    Returns:
    --------
    None
    """
    table.obs['genes_detected'] = table.X.getnnz(axis=1).astype('int32')


# Function to make string columns readable by viewer
def normalise_string_dtypes(table):
    """
    Rewrite pandas string-dtype columns as plain object columns.

    AnnData writes pandas `str` dtype as 'nullable-string-array', which is zarr
    group of mask and values arrays. Vitessce only reads plain 'string-array',
    so gene names and any string annotation stored like this load as empty and
    heatmap/gene list render nothing.

    Parameters:
    -----------
    table: anndata.AnnData
      Table element whose var index and obs columns are rewritten in place.

    Returns:
    --------
    converted: list of str
      Names of columns that were rewritten ('var index' for index).
    """
    converted = []
    if table.var.index.dtype != object:
        table.var.index = pd.Index(table.var.index.astype(object))
        converted.append('var index')
    for frame_name, frame in (('obs', table.obs), ('var', table.var)):
        for column in frame.columns:
            dtype = frame[column].dtype
            # Categoricals already write their categories as plain string-array,
            # and SpatialData requires region column to stay categorical
            if isinstance(dtype, pd.CategoricalDtype) or dtype == object:
                continue
            if pd.api.types.is_string_dtype(frame[column]):
                frame[column] = frame[column].astype(object)
                converted.append(f'{frame_name}/{column}')

    return converted


# Function to make integer obs columns loadable as viewer metrics
def downcast_int64_obs(table):
    """
    Rewrite int64 obs columns as int32 when their values fit.

    Zarr reader in viewer returns int64 as BigInt64Array, which its obs column
    loader cannot copy into Float32Array: load throws and every metric sharing
    that file definition comes up blank. Xenium counts peak in low thousands.

    Parameters:
    -----------
    table: anndata.AnnData
      Table element whose obs columns are rewritten in place.

    Returns:
    --------
    converted: list of str
      Names of obs columns that were downcast.
    """
    info = np.iinfo('int32')
    converted = []
    for column in table.obs.columns:
        values = table.obs[column]
        if values.dtype != 'int64':
            continue
        # Out-of-range column stays int64; config generator then skips it
        if values.min() < info.min or values.max() > info.max:
            continue
        table.obs[column] = values.astype('int32')
        converted.append(column)

    return converted


# Function to build clockwise quarter-turn rotation of image frame
def quarter_turn_matrix(degrees, width, height):
    """
    Build affine matrix rotating global pixel frame clockwise by quarter turns.

    y axis points down, so clockwise on screen. Translation keeps rotated
    image in positive quadrant with origin at its top-left corner, so image
    stays on identity transform.

    Parameters:
    -----------
    degrees: int
      Clockwise rotation: 90, 180 or 270.
    width: int
      Image width in global pixels, before rotation.
    height: int
      Image height in global pixels, before rotation.

    Returns:
    --------
    matrix: numpy.ndarray
      3x3 affine matrix acting on (x, y, 1) column vectors.
    """
    matrices = {
        90: [[0, -1, height], [1, 0, 0], [0, 0, 1]],
        180: [[-1, 0, width], [0, -1, height], [0, 0, 1]],
        270: [[0, 1, 0], [-1, 0, width], [0, 0, 1]],
    }

    return np.array(matrices[degrees], dtype='float64')


# Function to apply 3x3 affine matrix to polygons
def transform_geometry(geometry, matrix):
    """
    Apply affine matrix acting on (x, y, 1) to shapely geometries.

    Parameters:
    -----------
    geometry: geopandas.GeoSeries
      Polygons to transform.
    matrix: numpy.ndarray
      3x3 affine matrix, e.g. from quarter_turn_matrix().

    Returns:
    --------
    transformed: geopandas.GeoSeries
      Transformed polygons, same index.
    """
    m = matrix

    # shapely order: x' = a x + b y + xoff, y' = d x + e y + yoff
    return geometry.affine_transform([
        m[0, 0],
        m[0, 1],
        m[1, 0],
        m[1, 1],
        m[0, 2],
        m[1, 2],
    ])


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
def set_microns_per_pixel(out_path, image_name, microns):
    """
    Store microns per pixel of image's level s0 as image attribute.

    Config generator reads it for scale bar. Needed whenever shapes are in
    pixels rather than microns, so pixel size cannot be derived from them as
    for Xenium.

    Parameters:
    -----------
    out_path: str
      SpatialData .zarr store.
    image_name: str
      Image element name.
    microns: float
      Microns per pixel of level s0.

    Returns:
    --------
    None
    """
    group = zarr.open_group(
        os.path.join(out_path, 'images', image_name), mode='r+'
    )
    group.attrs.update(microns_per_pixel=microns)


# Function to read joint analysis rows of one sample
def read_joint_cells(joint_csv, sample_id):
    """
    Read joint analysis rows of one sample, indexed by its cell or spot id.

    Parameters:
    -----------
    joint_csv: str
      CSV with 'sample' and 'cell_id' columns, normally JOINT_KEY_COLUMNS
      and cell set columns.
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


# Function to copy joint analysis set lists into table's obs
def add_set_columns(obs, cells, ids):
    """
    Add every non-key column of joint analysis rows as categorical set list.

    Cells absent from analysis, or missing value, become 'Unassigned', since
    Vitessce's obsSets view cannot render missing values.

    Parameters:
    -----------
    obs: pandas.DataFrame
      Table's obs, gaining one column per set list in place.
    cells: pandas.DataFrame
      Joint analysis rows from read_joint_cells().
    ids: array-like
      Unprefixed cell ids in obs row order.

    Returns:
    --------
    None
    """
    set_cols = [c for c in cells.columns if c not in JOINT_KEY_COLUMNS]
    for column in set_cols:
        values = cells[column].reindex(ids).fillna('Unassigned').astype(str)
        obs[column] = pd.Categorical(values.to_numpy())
