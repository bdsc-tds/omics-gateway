"""
Script to generate Vitessce view-config JSON for SpatialData .zarr store.

Config references store through gateway's Zarr-serving route (default
/spatial-data/<path under spatial root>), so URLs are relative and resolve
same-origin when viewer page is served by gateway.

Run in `spatial` conda env, which holds both spatialdata (for conversion) and
vitessce (for this script).

Usage:
    python data_prep/generate_spatial_config.py \
        --zarr data/spatial/xenium/xenium_skin_1.zarr \
        --spatial-root data/spatial
"""

# Import utility modules
import argparse
import json
import math
import os

import numpy as np
import pandas as pd
import zarr
from anndata.io import read_elem
from PIL import Image
from vitessce import (
    AnnDataWrapper,
    SpatialDataWrapper,
    VitessceConfig,
    get_initial_coordination_scope_prefix,
)
from vitessce import CoordinationLevel as CL

# Bitmask label images and polygon shapes are both valid segmentations, but
# polygons avoid rasterising full-resolution mask during conversion
SEGMENTATION_GROUPS = ['labels', 'shapes']

# Each config holds one dataset, so fixed uid is enough and keeps derived
# coordination scope names stable
DATASET_UID = 'A'

# Layer name in viewer comes from obsType, so transcripts need one of their own
POINT_OBS_TYPE = 'transcript'

# Centroids pre-scaled to rendered coordinates, written by convert_xenium.py
CENTROIDS_KEY = 'spatial_global'

# Per-cell measurements offered for colouring, as obs columns: Xenium's own,
# then those convert_visium_hd.py computes
METRIC_OBS_COLS = [
    'cell_area',
    'nucleus_area',
    'nucleus_count',
    'transcript_counts',
    'umi_counts',
    'genes_detected',
]

# featureType keeping metrics apart from genes; also metric channel's obsType,
# which labels its layer row
METRIC_TYPE = 'metric'

# Config file suffixes; page derives sidecar path from config path likewise
CONFIG_SUFFIX = '.vitessce.json'
NO_METRICS_SUFFIX = '.nometrics.vitessce.json'
METRIC_VALUES_SUFFIX = '.metrics.json'

# Viewer decodes these as BigInt, which its column loader cannot store, so one
# such column silently blanks every metric
UNLOADABLE_DTYPES = {'int64', 'uint64'}

# Display names for obsm keys conventionally used for embeddings
EMBEDDING_NAMES = {
    'X_umap': 'UMAP',
    'X_umap_sample': 'Sample UMAP',
    'X_pca': 'PCA',
    'X_tsne': 't-SNE',
}

# Embedding type of per-sample UMAP in multi-sample stores, whose X_umap is
# joint analysis's
SAMPLE_UMAP = EMBEDDING_NAMES['X_umap_sample']

# Table of every sample's cells holding joint UMAP, per sample of
# multi-sample store (written by merge_study_tables.py)
ALL_CELLS_UMAP_SUFFIX = '_umap_all_cells'

# Sample switch entry and title of study-wide config of multi-sample store
ALL_SAMPLES_LABEL = 'All samples'

# Sample thumbnails beside study config: square crop of tissue, side in pixels
THUMBNAIL_SUFFIX = '.thumbnail.jpg'
THUMBNAIL_SIZE = 320

# Thumbnail margin around tissue, as fraction of tissue's longer side
THUMBNAIL_MARGIN = 0.04

# Tissue pixels: H&E stain's colour spread, or DAPI share of its 99.5th
# percentile; slide background, bubbles and dust fall below
HE_TISSUE_SPREAD = 30
DAPI_TISSUE_LEVEL = 0.15

# obs columns that describe cells rather than annotate them
NON_SET_OBS_COLS = {
    'cell_id',
    'region',
    'z_level',
    'segmentation_method',
    'transcript_counts',
    'control_probe_counts',
    'genomic_control_counts',
    'control_codeword_counts',
    'unassigned_codeword_counts',
    'deprecated_codeword_counts',
    'total_counts',
    'cell_area',
    'nucleus_area',
    'nucleus_count',
    'umi_counts',
    'genes_detected',
}


# Function to list child element names of zarr group directory
def list_group_members(group_dir):
    """
    List element names inside SpatialData zarr group directory.

    Parameters:
    -----------
    group_dir: str
      Path to group directory within store (e.g. <store>/images).

    Returns:
    --------
    members: list of str
      Sorted names of child elements, excluding zarr bookkeeping entries.
    """
    if not os.path.isdir(group_dir):
        return []
    skip = {'zarr.json', '.zgroup', '.zattrs', '.zmetadata', 'zmetadata'}

    return sorted(name for name in os.listdir(group_dir) if name not in skip)


# Function to read element's SpatialData coordinate-system name
def read_coordinate_system(element_dir):
    """
    Read output coordinate-system name of SpatialData zarr element.

    Name comes from element's NGFF coordinateTransformations (output.name),
    which SpatialData sets to element's coordinate system (e.g. per-sample or
    "global").

    Parameters:
    -----------
    element_dir: str
      Path to element directory within store (holds zarr.json).

    Returns:
    --------
    name: str or None
      Coordinate-system name, or None if it could not be determined.
    """
    meta_path = os.path.join(element_dir, 'zarr.json')
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path) as handle:
        attrs = json.load(handle).get('attributes', {})

    # Shapes/points store transformations at top level; images nest them under
    # ome/multiscales metadata
    transforms = attrs.get('coordinateTransformations')
    if not transforms:
        multiscales = attrs.get('ome', {}).get('multiscales', [])
        if multiscales:
            transforms = multiscales[0].get('coordinateTransformations')
    if not transforms:
        return None

    return transforms[0].get('output', {}).get('name')


# Function to read pixel dimensions of multiscale image element
def read_image_shape(image_dir):
    """
    Read (width, height) of image element's full-resolution level.

    Parameters:
    -----------
    image_dir: str
      Path to image element directory within store.

    Returns:
    --------
    shape: tuple of int or None
      (width, height) in pixels, or None if it could not be determined.
    """
    meta_path = os.path.join(image_dir, 'zarr.json')
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path) as handle:
        attrs = json.load(handle).get('attributes', {})
    multiscales = attrs.get('ome', {}).get('multiscales', [])
    if not multiscales:
        return None

    level = multiscales[0]['datasets'][0]['path']
    level_meta = os.path.join(image_dir, level, 'zarr.json')
    if not os.path.isfile(level_meta):
        return None
    with open(level_meta) as handle:
        # Axes are cyx, so last two entries are height then width
        shape = json.load(handle).get('shape')

    return (shape[-1], shape[-2]) if shape and len(shape) >= 2 else None


# Function to read size of viewer's world unit in microns
def read_microns_per_unit(zarr_path, image, seg_path):
    """
    Read how many microns one unit of store's coordinate frame spans.

    Viewer's scale bar needs it: frame unit is image pixel, not micron.
    Image attribute 'microns_per_pixel' wins when present; otherwise shapes'
    scale is used, as Xenium stores keep polygons in microns, scaled to
    image pixels.

    Parameters:
    -----------
    zarr_path: str
      Path to SpatialData .zarr store on disk.
    image: str
      Image element name.
    seg_path: str
      Segmentation element path within store (e.g. 'shapes/cell_boundaries').

    Returns:
    --------
    microns: float or None
      Microns per unit, or None when store gives no way to tell.
    """
    meta_path = os.path.join(zarr_path, 'images', image, 'zarr.json')
    if os.path.isfile(meta_path):
        with open(meta_path) as handle:
            attrs = json.load(handle).get('attributes', {})
        if attrs.get('microns_per_pixel'):
            return float(attrs['microns_per_pixel'])

    if not seg_path.startswith('shapes/'):
        return None
    meta_path = os.path.join(zarr_path, *seg_path.split('/'), 'zarr.json')
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path) as handle:
        attrs = json.load(handle).get('attributes', {})
    for transform in attrs.get('coordinateTransformations', []):
        scale = transform.get('scale')
        if transform.get('type') == 'scale' and scale and scale[0] == scale[-1]:
            return 1 / scale[0]

    return None


# Function to derive layer file uid from segmentation element name
def file_uid_of(element_name, sample=None):
    """
    Derive file uid from segmentation element name, without sample prefix.

    File uid also names extra layers' rows (e.g. 'nucleus'), so prefix of
    multi-sample stores is dropped.

    Parameters:
    -----------
    element_name: str
      Shapes or labels element name (e.g. 's1_nucleus_boundaries').
    sample: str or None
      Sample key prefixing element names, if any.

    Returns:
    --------
    file_uid: str
      Name without sample prefix and '_boundaries' suffix (e.g. 'nucleus').
    """
    if sample is not None:
        element_name = element_name.removeprefix(f'{sample}_')

    return element_name.replace('_boundaries', '')


# Function to pick metric obs columns viewer can load
def detect_metric_cols(obs_dir):
    """
    List metric obs columns present in table with dtype viewer can load.

    Columns are read by Vitessce's obsFeatureColumns loader, which copies them
    into Float32Array: int64 decodes as BigInt there and fails silently, so
    such columns are skipped with warning.

    Parameters:
    -----------
    obs_dir: str
      Path to table's obs group within store.

    Returns:
    --------
    metric_cols: list of str
      Names from METRIC_OBS_COLS that exist as loadable numeric arrays.
    """
    metric_cols = []
    for name in METRIC_OBS_COLS:
        meta_path = os.path.join(obs_dir, name, 'zarr.json')
        if not os.path.isfile(meta_path):
            continue
        with open(meta_path) as handle:
            data_type = json.load(handle).get('data_type')
        if data_type in UNLOADABLE_DTYPES:
            print(f'Skipping metric {name}: {data_type} cannot load in viewer')
            continue
        metric_cols.append(name)

    return metric_cols


# Function to write per-cell metric values shown in spatial tooltip
def write_metric_values(table_dir, metric_cols, out_path):
    """
    Write metric values keyed by cell id, for page script to add to tooltip.

    Vitessce tooltips list only obs id and cell-set membership, so viewer page
    looks hovered id up in this file instead.

    Parameters:
    -----------
    table_dir: str
      Path to table element within store.
    metric_cols: list of str
      obs columns to include, in metric list order.
    out_path: str
      Destination path for JSON file.

    Returns:
    --------
    out_path: str
      Path JSON was written to.
    """
    obs = read_elem(zarr.open_group(os.path.join(table_dir, 'obs'), mode='r'))
    values = obs[metric_cols].astype('float64').round(2)
    # NaN is not valid JSON; null reads as missing in page
    cells = {
        cell_id: [None if math.isnan(v) else v for v in row]
        for cell_id, row in zip(obs.index, values.itertuples(index=False))
    }
    with open(out_path, 'w') as handle:
        json.dump(
            {'columns': metric_cols, 'cells': cells},
            handle,
            separators=(',', ':'),
        )

    return out_path


# Function to list each set column's values in Vitessce's palette order
def read_set_order(table_dir, set_cols):
    """
    List values present in each set column, sorted as Vitessce sorts them.

    Vitessce gives each set palette colour of its position among values
    present in table, so set missing from one sample shifts later sets'
    colours there. Read from merged table, this order lets viewer page give
    each set same colour in every sample's config.

    Parameters:
    -----------
    table_dir: str
      Path to table element within store.
    set_cols: list of str
      obs columns shown as cell sets.

    Returns:
    --------
    order: dict
      Set names in palette order, keyed by hierarchy name.
    """
    order = {}
    for col in set_cols:
        values = read_elem(
            zarr.open_group(os.path.join(table_dir, 'obs', col), mode='r')
        )
        # Missing values belong to no set in Vitessce
        present = pd.Series(values).dropna().astype(str).unique()
        order[col.replace('_', ' ').title()] = sorted(present)

    return order


# Function to list samples of multi-sample store
def read_store_samples(zarr_path):
    """
    List samples of multi-sample store in display order, from merged table.

    Store counts as multi-sample only when merged table has sample column
    and every sample has own '<key>_table', so single-sample stores never get
    study-wide config.

    Parameters:
    -----------
    zarr_path: str
      Path to SpatialData .zarr store on disk.

    Returns:
    --------
    samples: list of dict
      'key', 'label' and 'disease' (None when column absent) per sample;
      empty for single-sample stores.
    """
    obs_dir = os.path.join(zarr_path, 'tables', 'table', 'obs')
    if not os.path.isdir(os.path.join(obs_dir, 'sample')):
        return []
    obs = zarr.open_group(obs_dir, mode='r')
    pairs = pd.DataFrame({
        'label': read_elem(obs['sample']),
        'key': read_elem(obs['region']).astype(str),
        'disease': (
            read_elem(obs['disease']).astype(str) if 'disease' in obs else None
        ),
    }).drop_duplicates()
    # Categories hold display order given to merge script
    pairs = pairs.sort_values('label')
    samples = [
        {
            'key': key.removesuffix('_cell_boundaries'),
            'label': str(label),
            'disease': disease,
        }
        for label, key, disease in zip(
            pairs['label'], pairs['key'], pairs['disease']
        )
    ]
    tables = list_group_members(os.path.join(zarr_path, 'tables'))
    if not all(f'{s["key"]}_table' in tables for s in samples):
        return []

    return samples


# Function to list samples of multi-sample store with their configs' names
def read_sample_configs(store_samples, config_name, sample):
    """
    List study-wide config, then store's samples, each with config's name.

    Config names derive from this config's name, swapping or dropping
    sample key, so metrics variant is kept.

    Parameters:
    -----------
    store_samples: list of dict
      Samples from read_store_samples().
    config_name: str
      File name of this config (e.g. 'study_s1.vitessce.json').
    sample: str or None
      Sample key of this config; None for study-wide config.

    Returns:
    --------
    label: str or None
      Display label of this config; None when config name lacks its key.
    samples: list of dict
      'label' and 'config' (file name beside this config) per entry.
    """
    if sample is None:
        study_config = config_name
    else:
        marker = f'_{sample}.'
        if marker not in config_name:
            return None, []
        study_config = config_name.replace(marker, '.', 1)
    # Store names hold no dot, so first one ends study stem
    stem, rest = study_config.split('.', 1)
    samples = [{'label': ALL_SAMPLES_LABEL, 'config': study_config}] + [
        {'label': s['label'], 'config': f'{stem}_{s["key"]}.{rest}'}
        for s in store_samples
    ]
    label = next(
        (s['label'] for s in samples if s['config'] == config_name), None
    )

    return label, samples


# Function to name sample's thumbnail after study config
def thumbnail_name(config_path, key):
    """
    Name sample's thumbnail file, shared by both metrics variants.

    Parameters:
    -----------
    config_path: str
      Path of study-wide config.
    key: str
      Sample key in store.

    Returns:
    --------
    name: str
      File name beside config.
    """
    stem = os.path.basename(config_path).split('.', 1)[0]

    return f'{stem}_{key}{THUMBNAIL_SUFFIX}'


# Function to write H&E or DAPI thumbnail of one sample
def write_thumbnail(image_dir, out_path):
    """
    Save square JPEG thumbnail of image, cropped to tissue.

    RGB image (H&E) keeps its colours; fluorescence image (Xenium) shows its
    first channel (DAPI) in grey, stretched to 99.5th percentile. Tissue's
    bounding box, trimmed of stray specks, is padded to square with image's
    background colour, so every sample's card shows same size and framing.
    Smallest pyramid level still twice thumbnail size is read.

    Parameters:
    -----------
    image_dir: str
      Path to image element directory within store.
    out_path: str
      Destination .jpg path.
    """
    with open(os.path.join(image_dir, 'zarr.json')) as handle:
        attrs = json.load(handle)['attributes']
    levels = [
        zarr.open_array(os.path.join(image_dir, d['path']), mode='r')
        for d in attrs['ome']['multiscales'][0]['datasets']
    ]
    level = next(
        (a for a in reversed(levels) if max(a.shape[1:]) >= 2 * THUMBNAIL_SIZE),
        levels[0],
    )
    pixels = level[:]
    if pixels.dtype == np.uint8 and pixels.shape[0] == 3:
        # Axes are cyx; PIL wants yxc
        image = np.moveaxis(pixels, 0, -1)
        spread = image.max(axis=-1).astype('int16') - image.min(axis=-1)
        tissue = spread > HE_TISSUE_SPREAD
    else:
        dapi = pixels[0].astype('float32')
        top = max(float(np.percentile(dapi, 99.5)), 1.0)
        image = (np.clip(dapi / top, 0, 1) * 255).astype('uint8')
        tissue = image > DAPI_TISSUE_LEVEL * 255
    background = (
        np.median(image[~tissue], axis=0).astype('uint8')
        if (~tissue).any()
        else image[0, 0]
    )

    ys, xs = np.nonzero(tissue)
    if len(ys):
        y0, y1 = np.percentile(ys, [0.5, 99.5]).astype(int)
        x0, x1 = np.percentile(xs, [0.5, 99.5]).astype(int)
    else:
        y0, y1, x0, x1 = 0, image.shape[0] - 1, 0, image.shape[1] - 1
    side = int(max(y1 - y0, x1 - x0) * (1 + 2 * THUMBNAIL_MARGIN)) + 1
    canvas = np.empty((side, side, *image.shape[2:]), dtype='uint8')
    canvas[...] = background
    # Tissue centred on canvas; parts of image beyond canvas are dropped
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    top_y, left_x = cy - side // 2, cx - side // 2
    src_y0, src_x0 = max(top_y, 0), max(left_x, 0)
    src_y1 = min(top_y + side, image.shape[0])
    src_x1 = min(left_x + side, image.shape[1])
    canvas[
        src_y0 - top_y : src_y1 - top_y, src_x0 - left_x : src_x1 - left_x
    ] = image[src_y0:src_y1, src_x0:src_x1]

    thumbnail = Image.fromarray(canvas).resize(
        (THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS
    )
    thumbnail.save(out_path, quality=85)


# Function to compute zoom level that fits extent in viewport
def fit_zoom(image_shape, viewport=(800, 450)):
    """
    Compute Vitessce zoom level that fits image in spatial view.

    Vitessce renders one world unit per 2**zoom screen pixels and opens on
    default zoom which leaves images too small, so fitting it here frames
    dataset automatically. Viewer's main.js recognises this reference fit and
    refits it to real panel at load, so default viewport must match its
    REFERENCE_PANEL.

    Parameters:
    -----------
    image_shape: tuple of int or None
      (width, height) of image in pixels.
    viewport: tuple of int
      Approximate (width, height) of spatial view in screen pixels.

    Returns:
    --------
    zoom: float or None
      Zoom level, or None when image size is unknown.
    """
    if not image_shape:
        return None

    return math.log2(
        min(viewport[0] / image_shape[0], viewport[1] / image_shape[1])
    )


# Function to auto-detect element paths of spatial store
def detect_elements(zarr_path, image=None, segmentations=None, sample=None):
    """
    Pick image, segmentation and table element paths from store.

    Multi-sample stores prefix each sample's elements with its key
    (e.g. 's1_he', 's1_cell_boundaries', 's1_table'); sample restricts
    detection to those, so other samples never appear as extra layers.

    Parameters:
    -----------
    zarr_path: str
      Path to SpatialData .zarr store on disk.
    image: str or None
      Image element name to use; first one found when None.
    segmentations: str or None
      Segmentation element name; first labels or shapes element when None.
    sample: str or None
      Sample key of multi-sample store; whole store when None.

    Returns:
    --------
    paths: dict
      Keys 'image_shape', 'image_path', 'microns_per_unit',
      'obs_segmentations_path', 'other_segmentation_paths', 'table_path',
      'obs_points_path', 'coordinate_system', 'obs_set_cols',
      'obs_embeddings', 'centroids_path', 'metric_cols' and
      'all_cells_umap_path'.
    """

    # Function to list group members, restricted to sample's elements
    def members_of(group):
        """
        List element names of store group, keeping only sample's when set.

        Parameters:
        -----------
        group: str
          Group name within store (e.g. 'images').

        Returns:
        --------
        names: list of str
          Sorted element names.
        """
        names = list_group_members(os.path.join(zarr_path, group))
        if sample is None:
            return names
        return [name for name in names if name.startswith(f'{sample}_')]

    images = members_of('images')
    tables = members_of('tables')
    points = members_of('points')

    if not images:
        raise ValueError(f'No image elements found in {zarr_path}/images')
    if not tables:
        raise ValueError(f'No table elements found in {zarr_path}/tables')

    image = image or images[0]

    # Prefer segmentation representation currently held by store
    seg_path = None
    other_seg_paths = []
    for group in SEGMENTATION_GROUPS:
        members = members_of(group)
        if not members:
            continue
        name = segmentations or members[0]
        if name in members:
            seg_path = f'{group}/{name}'
            # Nuclei and any further segmentation become their own layers, each
            # needing separate file def since one file def carries one path
            other_seg_paths = [
                f'{group}/{other}' for other in members if other != name
            ]
            break
    if seg_path is None:
        raise ValueError(f'No labels or shapes elements found in {zarr_path}')

    # SpatialData convention places annotation table at tables/table
    table_name = 'table' if 'table' in tables else tables[0]
    if sample is not None and f'{sample}_table' in tables:
        table_name = f'{sample}_table'

    coordinate_system = (
        read_coordinate_system(os.path.join(zarr_path, *seg_path.split('/')))
        or read_coordinate_system(os.path.join(zarr_path, 'images', image))
        or 'global'
    )

    table_dir = os.path.join(zarr_path, 'tables', table_name)
    all_cells_umap = f'{sample}{ALL_CELLS_UMAP_SUFFIX}'

    return {
        'image_shape': read_image_shape(
            os.path.join(zarr_path, 'images', image)
        ),
        'image_path': f'images/{image}',
        'microns_per_unit': read_microns_per_unit(zarr_path, image, seg_path),
        'obs_segmentations_path': seg_path,
        'other_segmentation_paths': other_seg_paths,
        'table_path': f'tables/{table_name}',
        'obs_points_path': f'points/{points[0]}' if points else None,
        'coordinate_system': coordinate_system,
        'obs_set_cols': [
            name
            for name in list_group_members(os.path.join(table_dir, 'obs'))
            if name not in NON_SET_OBS_COLS and not name.startswith('_')
        ],
        # Centroid arrays hold positions, not embeddings
        'obs_embeddings': [
            name
            for name in list_group_members(os.path.join(table_dir, 'obsm'))
            if name not in ('spatial', CENTROIDS_KEY)
        ],
        # Absent from stores written before centroids step was added
        'centroids_path': (
            f'obsm/{CENTROIDS_KEY}'
            if os.path.isdir(os.path.join(table_dir, 'obsm', CENTROIDS_KEY))
            else None
        ),
        'metric_cols': detect_metric_cols(os.path.join(table_dir, 'obs')),
        'all_cells_umap_path': (
            f'tables/{all_cells_umap}' if all_cells_umap in tables else None
        ),
    }


# Function to unbind extra segmentations from cells' annotation table
def drop_extra_segmentation_table_paths(config, extra_file_uids):
    """
    Strip tablePath from file defs of segmentations that no table annotates.

    Loader prefers tablePath over element's own index when both exist, so extra
    segmentation inherits cells' obs index, mismatched in both ids and length.
    Dropping it makes loader read element's own parquet index instead.
    SpatialDataWrapper emits tablePath whether or not table_path is passed, so
    it can only be removed after config is built.

    Matching is on fileUid rather than obsType because extra segmentations
    share primary's obsType, so obsType cannot tell their file defs apart.

    Parameters:
    -----------
    config: dict
      Vitessce config, modified in place.
    extra_file_uids: list of str
      fileUid of each extra segmentation, identifying its file def.

    Returns:
    --------
    n_stripped: int
      Number of file defs whose tablePath was removed.
    """
    n_stripped = 0
    for dataset in config.get('datasets', []):
        for file_def in dataset.get('files', []):
            file_uid = file_def.get('coordinationValues', {}).get('fileUid')
            if file_uid not in extra_file_uids:
                continue
            options = file_def.get('options', {}).get('obsSegmentations', {})
            if options.pop('tablePath', None) is not None:
                n_stripped += 1

    return n_stripped


# Function to replace layout views by one tabs view in first one's slot
def replace_with_tabs(config, labelled_views):
    """
    Replace given layout entries with one gatewayTabs view, in first's slot.

    Tabs view is gateway's own plugin (spatial_viewer_src/gateway_views.js).
    Each tab keeps its view's coordination scopes and props, which viewer
    applies over tabs view's auto-initialised scopes.

    Parameters:
    -----------
    config: dict
      Vitessce config, modified in place.
    labelled_views: list of tuple
      (layout entry, tab label) pairs, in tab order.
    """
    first = labelled_views[0][0]
    tabs = {
        'component': 'gatewayTabs',
        **{key: first[key] for key in ('x', 'y', 'w', 'h')},
        'coordinationScopes': {},
        'props': {
            'tabs': [
                {
                    'label': label,
                    'component': view['component'],
                    'coordinationScopes': view['coordinationScopes'],
                    'props': view.get('props', {}),
                }
                for view, label in labelled_views
            ]
        },
    }
    nested = [id(view) for view, _ in labelled_views]
    config['layout'] = [
        view for view in config['layout'] if id(view) not in nested
    ] + [tabs]


# Function to put dot plot and violin plot in one panel, as tabs
def nest_plot_tabs(config):
    """
    Put dot plot and violin plot in dot plot's slot, as tabs.

    Each tab keeping its own scopes is what leaves dot plot's aggregation
    strategy unshared.

    Parameters:
    -----------
    config: dict
      Vitessce config, modified in place.
    """
    views = {view['component']: view for view in config['layout']}
    replace_with_tabs(
        config,
        [
            (views['dotPlot'], 'Dot plot'),
            (views['obsSetFeatureValueDistribution'], 'Violin plot'),
        ],
    )


# Function to put merged and per-sample UMAPs in one panel, as tabs
def nest_umap_tabs(config, all_cells=False):
    """
    Put scatterplots of merged and sample UMAP in one panel, as tabs.

    Views are told apart by embedding type, as vitessce-python gives views
    no uid. Each tab gets own pan and zoom scopes, left null so it fits its
    embedding on first show; tabs view's single auto-initialised ones would
    open one embedding on other's framing. Configs without sample UMAP are
    left unchanged.

    Parameters:
    -----------
    config: dict
      Vitessce config, modified in place.
    all_cells: bool
      Whether merged UMAP shows every sample's cells; its points are then
      made opaque, as automatic opacity, set by point count, washed out
      sample's colours over other samples' grey.
    """
    space = config['coordinationSpace']
    embedding_types = space.get('embeddingType', {})
    by_type = {
        embedding_types.get(
            view['coordinationScopes'].get('embeddingType')
        ): view
        for view in config['layout']
        if view['component'] == 'scatterplot'
    }
    if SAMPLE_UMAP not in by_type:
        return
    views = [by_type['UMAP'], by_type[SAMPLE_UMAP]]
    for view in views:
        scope = view['coordinationScopes']['embeddingType']
        for name in (
            'embeddingZoom',
            'embeddingTargetX',
            'embeddingTargetY',
            'embeddingTargetZ',
        ):
            space.setdefault(name, {})[scope] = None
            view['coordinationScopes'][name] = scope
    if all_cells:
        scope = views[0]['coordinationScopes']['embeddingType']
        for name, value in (
            ('embeddingObsOpacityMode', 'manual'),
            ('embeddingObsOpacity', 1.0),
        ):
            space.setdefault(name, {})[scope] = value
            views[0]['coordinationScopes'][name] = scope
    replace_with_tabs(
        config, [(views[0], 'Merged UMAP'), (views[1], 'Sample UMAP')]
    )


# Function to build and write Vitessce config for SpatialData store
def generate_config(
    zarr_path,
    serve_base,
    out_path,
    image=None,
    segmentations=None,
    obs_type='cell',
    metrics=True,
    sample=None,
):
    """
    Build Vitessce view-config for SpatialData store and write it to disk.

    Layout mirrors reference Vitessce demos: spatial view with its layer
    controller on left, and scatterplot, cell sets, expression dot plot and gene
    list alongside: https://vitessce.io/#?dataset=codeluppi-2018

    Whole multi-sample store (no sample) gets study layout on same grid:
    sample thumbnails, written beside config, in controller's slot, then
    merged table's UMAP coloured by sample and same UMAP coloured by
    selection, swapping widths of spatial view and UMAP.

    Parameters:
    -----------
    zarr_path: str
      Path to SpatialData .zarr store on disk.
    serve_base: str
      URL of folder holding store (e.g. /spatial-data/xenium).
    out_path: str
      Destination path for generated config JSON.
    image: str or None
      Image element override passed to detection.
    segmentations: str or None
      Segmentation element override passed to detection.
    obs_type: str
      Label used for observations throughout views.
    metrics: bool
      Whether to add Metric layer and its list, which replaces Status panel.
    sample: str or None
      Sample key of multi-sample store; None for whole store.

    Returns:
    --------
    out_path: str
      Path config JSON was written to.
    """
    name = os.path.basename(os.path.normpath(zarr_path))
    sdata_url = f'{serve_base.rstrip("/")}/{name}'
    paths = detect_elements(
        zarr_path, image=image, segmentations=segmentations, sample=sample
    )
    if not metrics:
        paths['metric_cols'] = []
    store_samples = read_store_samples(zarr_path)
    # Whole multi-sample store: merged table's UMAPs, plots and sample
    # thumbnails, no spatial view, as detected elements are first sample's
    study = sample is None and bool(store_samples)
    if study:
        paths.update(
            other_segmentation_paths=[],
            obs_points_path=None,
            centroids_path=None,
            # No spatial view for metrics to colour
            metric_cols=[],
        )
    config_label, sample_configs = (
        read_sample_configs(store_samples, os.path.basename(out_path), sample)
        if store_samples
        else (None, [])
    )

    table_path = paths['table_path']
    region = paths['obs_segmentations_path'].split('/')[-1]
    # Merged UMAP of all cells replaces sample's own cells' copy
    embeddings = [
        e
        for e in paths['obs_embeddings']
        if not (paths['all_cells_umap_path'] and e == 'X_umap')
    ]

    vc = VitessceConfig(schema_version='1.0.17', name=name)
    # Segmentations get own file defs below: each needs own fileUid, and one
    # set here would apply to image and table views too, blanking image
    wrapper = SpatialDataWrapper(
        sdata_url=sdata_url,
        image_path=None if study else paths['image_path'],
        table_path=table_path,
        coordinate_system=paths['coordinate_system'],
        # Table-derived views are inherited from AnnDataWrapper, so their paths
        # are given relative to store rather than to table
        obs_feature_matrix_path=f'{table_path}/X',
        obs_set_paths=[f'{table_path}/obs/{c}' for c in paths['obs_set_cols']],
        obs_set_names=[
            c.replace('_', ' ').title() for c in paths['obs_set_cols']
        ],
        obs_embedding_paths=[f'{table_path}/obsm/{e}' for e in embeddings],
        obs_embedding_names=[EMBEDDING_NAMES.get(e, e) for e in embeddings],
        coordination_values={'obsType': obs_type},
    )
    # uid is fixed because layer scope names are derived from it, and viewer
    # skips its own auto-initialisation only when it finds those exact names
    dataset = vc.add_dataset(name=name, uid=DATASET_UID).add_object(wrapper)
    # Other samples' cells have no sets or expression here, so they draw in
    # default grey behind sample's, which their table lists last
    if paths['all_cells_umap_path']:
        dataset.add_object(
            AnnDataWrapper(
                adata_url=f'{sdata_url}/{paths["all_cells_umap_path"]}',
                obs_embedding_paths=['obsm/X_umap'],
                obs_embedding_names=[EMBEDDING_NAMES['X_umap']],
                coordination_values={'obsType': obs_type},
            )
        )

    # Cells keep table so spatial view can colour them by set or gene
    primary_file_uid = file_uid_of(region, sample)
    if not study:
        dataset.add_object(
            SpatialDataWrapper(
                sdata_url=sdata_url,
                obs_segmentations_path=paths['obs_segmentations_path'],
                table_path=table_path,
                coordinate_system=paths['coordinate_system'],
                region=region,
                coordination_values={
                    'obsType': obs_type,
                    'fileUid': primary_file_uid,
                },
            )
        )

    # Transcripts get their own file def and obsType. Sharing cells' file def
    # made viewer label point layer 'Cell', since layer name comes from obsType
    if paths['obs_points_path']:
        dataset.add_object(
            SpatialDataWrapper(
                sdata_url=sdata_url,
                obs_points_path=paths['obs_points_path'],
                # Kept so feature index resolves against gene names
                table_path=table_path,
                coordinate_system=paths['coordinate_system'],
                # Defaults resolve via fragile spatialdata_attrs.feature_key;
                # naming them stops fast path degrading to whole-file fetch
                obs_points_feature_index_column='feature_name_codes',
                obs_points_morton_code_column='morton_code_2d',
                coordination_values={'obsType': POINT_OBS_TYPE},
            )
        )

    # No obsLocations SpatialData file type exists, so table is wrapped again
    # as plain AnnData; its loader applies no transform, hence pre-scaled key
    if paths['centroids_path']:
        dataset.add_object(
            AnnDataWrapper(
                adata_url=f'{sdata_url}/{table_path}',
                obs_locations_path=paths['centroids_path'],
                coordination_values={'obsType': obs_type},
            )
        )

    # Metrics colour second layer over cells: lookup keys on channel's
    # featureType, and polygon layer draws only its first channel
    if paths['metric_cols']:
        # SpatialDataWrapper drops obs_feature_column_paths, hence plain AnnData
        dataset.add_object(
            AnnDataWrapper(
                adata_url=f'{sdata_url}/{table_path}',
                obs_feature_column_paths=[
                    f'obs/{c}' for c in paths['metric_cols']
                ],
                coordination_values={
                    'obsType': METRIC_TYPE,
                    'featureType': METRIC_TYPE,
                },
            )
        )

    # Extra segmentations carry no table, so they get their own file def, but
    # they must keep views' obsType to render at all
    extra_file_uids = []
    for seg_path in paths['other_segmentation_paths']:
        extra_file_uid = file_uid_of(seg_path.split('/')[-1], sample)
        extra_file_uids.append(extra_file_uid)
        dataset.add_object(
            SpatialDataWrapper(
                sdata_url=sdata_url,
                obs_segmentations_path=seg_path,
                coordinate_system=paths['coordinate_system'],
                # obsType must match views', or layer is dropped (loads, never
                # renders); fileUid then tells same-type file defs apart
                coordination_values={
                    'obsType': obs_type,
                    'fileUid': extra_file_uid,
                },
            )
        )

    if study:
        # Gateway's own view: clicking sample opens its config
        thumbnails = vc.add_view('gatewaySampleThumbnails', dataset=dataset)
        thumbnails.set_props(
            samples=[
                {
                    'label': s['label'],
                    'disease': s['disease'],
                    'config': entry['config'],
                    'thumbnail': thumbnail_name(out_path, s['key']),
                }
                # First entry is this config
                for s, entry in zip(store_samples, sample_configs[1:])
            ]
        )
        spatial_views = []
    else:
        spatial = vc.add_view('spatialBeta', dataset=dataset)
        controller = vc.add_view('layerControllerBeta', dataset=dataset)
        # Sub-row per selected gene under Transcript row, with colour picker
        # and button removing gene from selection
        controller.set_props(layerPerFeatureForPoints=True)
        spatial_views = [spatial, controller]
    scatterplot = vc.add_view('scatterplot', dataset=dataset, mapping='UMAP')
    # Titles replace "Scatterplot (<mapping>)"
    scatterplot.set_props(title='UMAP')
    # Second scatterplot in same slot, joined to first as tab after to_dict
    umap_views = [scatterplot]
    if study:
        # Same embedding beside it, coloured by sample whatever is selected;
        # no lasso, which would replace that colouring for good
        by_sample = vc.add_view(
            'scatterplot', dataset=dataset, mapping='UMAP'
        ).set_props(title='UMAP by Sample', hideTools=True)
    elif 'X_umap_sample' in paths['obs_embeddings']:
        scatterplot.set_props(title='Merged UMAP')
        umap_views.append(
            vc.add_view(
                'scatterplot', dataset=dataset, mapping=SAMPLE_UMAP
            ).set_props(title=SAMPLE_UMAP)
        )
    obs_sets = vc.add_view('obsSets', dataset=dataset)
    # Mean expression and fraction expressing per cell set, for selected genes
    # only; heatmap always drew all genes and loaded whole matrix
    dot_plot = vc.add_view('dotPlot', dataset=dataset)
    # Worded like violin plot's "Expression by Cell Set"
    dot_plot.set_props(title='Mean Expression by Cell Set')
    feature_list = vc.add_view('featureList', dataset=dataset)
    # Shift-click enables multi-gene selection with distinct transcript colours
    # Defaults false for single-select behavior and no deselection
    feature_list.set_props(enableMultiSelect=True)
    distribution = vc.add_view(
        'obsSetFeatureValueDistribution', dataset=dataset
    )
    # Gateway's own view: cell types of lasso regions or selected sets
    breakdown = vc.add_view('gatewayCellTypeCounts', dataset=dataset)
    # Rows follow whichever cell-type list sets are ticked from, so its sets
    # compare by own types; default is first set list only
    cell_type_lists = [
        c.replace('_', ' ').title()
        for c in paths['obs_set_cols']
        if c.startswith('cell_type')
    ]
    if len(cell_type_lists) > 1:
        breakdown.set_props(rowHierarchies=cell_type_lists)
    description = vc.add_view('description', dataset=dataset)
    description.set_props(description=os.path.splitext(name)[0])
    # Metric list takes status panel's slot, least used one
    if paths['metric_cols']:
        side_panel = vc.add_view('featureList', dataset=dataset)
        vc.link_views(
            [side_panel], ['obsType', 'featureType'], [METRIC_TYPE, METRIC_TYPE]
        )
    else:
        side_panel = vc.add_view('status', dataset=dataset)

    # Every view shares one obsType, or they will not select each other's cells
    vc.link_views(
        [
            *spatial_views,
            *umap_views,
            *([by_sample] if study else []),
            obs_sets,
            dot_plot,
            feature_list,
            distribution,
            breakdown,
        ],
        ['obsType'],
        [obs_type],
    )

    # Colour encoding and selections are shared scope objects, not plain
    # values, so picking gene or cell set elsewhere recolours spatial view
    (
        color_encoding,
        feature_selection,
        obs_set_selection,
        obs_set_color,
        colormap_range,
        aggregation_strategy,
        obs_highlight,
    ) = vc.add_coordination(
        'obsColorEncoding',
        'featureSelection',
        'obsSetSelection',
        'obsSetColor',
        'featureValueColormapRange',
        'featureAggregationStrategy',
        'obsHighlight',
    )
    color_encoding.set_value('cellSetSelection')
    feature_selection.set_value(None)
    obs_set_selection.set_value(None)
    obs_set_color.set_value(None)
    obs_highlight.set_value(None)
    colormap_range.set_value([0.0, 1.0])
    # Multi-gene selection requires combining continuous views; default 'first'
    # otherwise silently ignores additional genes
    aggregation_strategy.set_value('mean')

    for view in (
        *spatial_views,
        *umap_views,
        obs_sets,
        feature_list,
        distribution,
    ):
        view.use_coordination(
            color_encoding,
            feature_selection,
            obs_set_selection,
            obs_set_color,
            colormap_range,
            aggregation_strategy,
            obs_highlight,
        )
    # Clicking dot writes gene's index into aggregation strategy, which would
    # switch every other view to that one gene, so that scope stays unshared
    dot_plot.use_coordination(
        feature_selection, obs_set_selection, obs_set_color
    )
    breakdown.use_coordination(obs_set_selection, obs_set_color)
    if store_samples:
        # Saved sets declared, so sample switch carries them by scope name;
        # auto-init gave views absent from other layout their own empty scope
        (saved_sets,) = vc.add_coordination('additionalObsSets')
        saved_sets.set_value(None)
        # Spatial view only: layer controller shows no sets
        set_views = [*spatial_views[:1], *umap_views, obs_sets, dot_plot]
        set_views += [distribution, breakdown]
        for view in set_views + ([by_sample] if study else []):
            view.use_coordination(saved_sets)
    if study:
        # Created after shared scopes, which so keep sample configs' names
        # for selections carried by sample switch
        sample_encoding, sample_selection = vc.add_coordination(
            'obsColorEncoding', 'obsSetSelection'
        )
        sample_encoding.set_value('cellSetSelection')
        sample_selection.set_value([
            ['Sample', s['label']] for s in store_samples
        ])
        by_sample.use_coordination(
            sample_encoding, sample_selection, obs_set_color, obs_highlight
        )
        # Pan and zoom together; null so first view fits embedding
        vc.link_views(
            [scatterplot, by_sample],
            ['embeddingZoom', 'embeddingTargetX', 'embeddingTargetY'],
            [None, None, None],
        )

    # Cells are annotated, so their channel reuses shared selection scopes;
    # extra segmentations have no table and only carry fixed colour
    segmentation_layers = [
        {
            'obsType': obs_type,
            # Both segmentation layers share obsType, so every layer needs its
            # own fileUid; layer without one resolves to wrong file def
            'fileUid': primary_file_uid,
            'spatialLayerVisible': True,
            'spatialLayerOpacity': 1,
            'segmentationChannel': CL([
                {
                    'obsType': obs_type,
                    'spatialTargetC': 0,
                    'spatialChannelColor': [255, 255, 255],
                    'spatialChannelOpacity': 1,
                    'spatialChannelVisible': True,
                    # Outlines keep morphology image visible; 3.75 is 75% of
                    # controller slider's 0.01 to 5.0 world-unit range
                    'spatialSegmentationFilled': False,
                    'spatialSegmentationStrokeWidth': 3.75,
                    'obsColorEncoding': color_encoding,
                    'featureSelection': feature_selection,
                    'obsSetSelection': obs_set_selection,
                    'obsSetColor': obs_set_color,
                    'featureValueColormapRange': colormap_range,
                    # Read per channel, not per view; draws tooltip and
                    # crosshair at centroid, never recolours polygon
                    'obsHighlight': obs_highlight,
                    'tooltipsVisible': True,
                    'tooltipCrosshairsVisible': True,
                    # Per channel, so Legend Visible acts on this layer alone
                    'legendVisible': True,
                }
            ]),
        }
    ]
    if paths['metric_cols']:
        # Own scopes shared only with metric list, so picking metric leaves
        # gene colouring and its colormap range alone
        metric_encoding, metric_selection, metric_range = vc.add_coordination(
            'obsColorEncoding', 'featureSelection', 'featureValueColormapRange'
        )
        metric_encoding.set_value('geneSelection')
        # Preselected so layer shows something as soon as it is switched on
        metric_selection.set_value(paths['metric_cols'][:1])
        metric_range.set_value([0.0, 1.0])
        side_panel.use_coordination(metric_encoding, metric_selection)
        # Below cells: their unfilled interiors still catch hover, so cell
        # tooltip and UMAP crosshair survive with this layer on
        segmentation_layers.insert(
            0,
            {
                'obsType': obs_type,
                # Reuses cells' file def: same element, so polygons load once
                'fileUid': primary_file_uid,
                'spatialLayerVisible': True,
                'spatialLayerOpacity': 1,
                'segmentationChannel': CL([
                    {
                        # Matches metric file def; also labels row "Metric"
                        'obsType': METRIC_TYPE,
                        'featureType': METRIC_TYPE,
                        'spatialTargetC': 0,
                        'spatialChannelColor': [255, 255, 255],
                        'spatialChannelOpacity': 1,
                        # Off by default: filled layer hides morphology image
                        'spatialChannelVisible': False,
                        'spatialSegmentationFilled': True,
                        'spatialSegmentationStrokeWidth': 1,
                        'obsColorEncoding': metric_encoding,
                        'featureSelection': metric_selection,
                        'featureValueColormapRange': metric_range,
                        # Own scope, as for extra segmentations below
                        'obsHighlight': None,
                        # Picked only while Cell layer is hidden; page adds
                        # metric values to tooltip
                        'tooltipsVisible': True,
                        'tooltipCrosshairsVisible': True,
                        'legendVisible': True,
                    }
                ]),
            },
        )
    # Extras appended last so they draw on top of filled cells; controller
    # lists layers reversed, so this also shows Nucleus above Cell
    segmentation_layers += [
        {
            'obsType': obs_type,
            'fileUid': extra_file_uid,
            # Eye toggles channel, not layer: hidden layer could not return;
            # off by default lives in spatialChannelVisible below
            'spatialLayerVisible': True,
            'spatialLayerOpacity': 1,
            'segmentationChannel': CL([
                {
                    # Channel obsType only sets row label; layer and file def
                    # keep views' obsType, so footer skips these segmentations
                    'obsType': extra_file_uid,
                    'spatialTargetC': 0,
                    # Deep blue (#00008B): mid blue is lost against bright DAPI
                    'spatialChannelColor': [0, 0, 139],
                    'spatialChannelOpacity': 1,
                    # Off by default: nuclei sit inside cells
                    'spatialChannelVisible': False,
                    # Filled, not outlined: stroke width is in world units, so
                    # 1-unit outline is ~0.01 screen px at default zoom
                    'spatialSegmentationFilled': True,
                    'spatialSegmentationStrokeWidth': 1,
                    'obsColorEncoding': 'spatialChannelColor',
                    # Own scope: hover clears undeclared highlights, which then
                    # resolve to cells' shared scope and wipe their tooltip
                    'obsHighlight': None,
                    # Own scope, so this layer's legend toggles alone
                    'legendVisible': True,
                }
            ]),
        }
        for extra_file_uid in extra_file_uids
    ]

    # Image layer left to viewer's own initialisation, which derives contrast
    # windows from pixel statistics this script cannot read without pyramid
    if not study:
        vc.link_views_by_dict(
            [spatial, controller],
            {'segmentationLayer': CL(segmentation_layers)},
            scope_prefix=get_initial_coordination_scope_prefix(
                DATASET_UID, 'obsSegmentations'
            ),
        )

    # Transcripts are off by default: millions of points obscure everything
    # beneath and are only meaningful once genes are selected
    if paths['obs_points_path']:
        vc.link_views_by_dict(
            [spatial, controller],
            {
                'pointLayer': CL([
                    {
                        'obsType': POINT_OBS_TYPE,
                        'spatialLayerVisible': False,
                        # Half brightness: points are dense enough at full
                        # opacity to hide morphology image under them
                        'spatialLayerOpacity': 0.5,
                        # Colour per gene from featureColor; unlike
                        # randomByFeature, its swatches can be edited
                        'obsColorEncoding': 'geneSelection',
                        # Vitessce's light-theme default, declared so row's
                        # picker can change it while no gene is selected
                        'spatialLayerColor': [200, 200, 200],
                        # Filled in per selected gene by viewer, from
                        # Vitessce's palette by gene position
                        'featureColor': [],
                        # Draw only genes picked in gene list, not all millions of
                        # detections. Checkbox in viewer sets this same string
                        'featureFilterMode': 'featureSelection',
                        'featureSelection': feature_selection,
                        # Own scope, as for extra segmentations above
                        'obsHighlight': None,
                    }
                ])
            },
            scope_prefix=get_initial_coordination_scope_prefix(
                DATASET_UID, 'obsPoints'
            ),
        )

    # Channel names read horizontally, like rest of controller; only this
    # property is set, so viewer still initialises channels and contrast
    if not study:
        vc.link_views_by_dict(
            [spatial, controller],
            {
                'imageLayer': CL([
                    {'spatialChannelLabelsOrientation': 'horizontal'}
                ])
            },
            scope_prefix=get_initial_coordination_scope_prefix(
                DATASET_UID, 'image'
            ),
        )

    # Vitessce otherwise opens zoomed too far out
    zoom = None if study else fit_zoom(paths['image_shape'])
    if zoom is not None:
        width, height = paths['image_shape']
        vc.link_views(
            [spatial, controller],
            ['spatialZoom', 'spatialTargetX', 'spatialTargetY'],
            [zoom, width / 2, height / 2],
        )

    # Metric list sits above Description; Status stays below it
    upper, lower = (
        (side_panel, description)
        if paths['metric_cols']
        else (description, side_panel)
    )
    # Grid mirrors codeluppi-2018 (vc.layout() only makes equal panels);
    # scatterplot fills both its embedding slots, Xenium having one embedding
    top = (
        # Study: thumbnails, then UMAP by sample beside them and UMAP beside
        # set lists, which colour it
        (
            (thumbnails, (0, 0, 3, 4)),
            (by_sample, (3, 0, 3, 4)),
            (scatterplot, (6, 0, 4, 4)),
        )
        if study
        else (
            (controller, (0, 0, 3, 4)),
            (spatial, (3, 0, 4, 4)),
            *((view, (7, 0, 3, 4)) for view in umap_views),
        )
    )
    for view, (x, y, w, h) in (
        *top,
        (upper, (0, 4, 3, 1)),
        (lower, (0, 5, 3, 1)),
        (feature_list, (10, 0, 2, 2)),
        (obs_sets, (10, 2, 2, 2)),
        (dot_plot, (3, 4, 5, 2)),
        (breakdown, (8, 4, 4, 2)),
    ):
        view.set_xywh(x, y, w, h)

    config = vc.to_dict()
    # Read by viewer's main.js, which removes it before Vitessce sees config
    options = {}
    if paths['microns_per_unit'] and not study:
        options['micronsPerUnit'] = paths['microns_per_unit']
    # Sets take merged table's colours, same in every sample
    if store_samples:
        options['setOrder'] = read_set_order(
            os.path.join(zarr_path, 'tables', 'table'), paths['obs_set_cols']
        )
        # Viewer's sample switch lists study config and every sample, in
        # title bar of panel named here
        if config_label is not None:
            options['sample'] = config_label
            options['samples'] = sample_configs
            options['switchTitle'] = 'UMAP' if study else 'Spatial'
    if options:
        config['gatewayOptions'] = options
    drop_extra_segmentation_table_paths(config, extra_file_uids)
    # Violin plot had no grid slot: it joins dot plot's as tab
    nest_plot_tabs(config)
    nest_umap_tabs(config, all_cells=bool(paths['all_cells_umap_path']))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w') as handle:
        json.dump(config, handle, indent=2)
    if study:
        images = list_group_members(os.path.join(zarr_path, 'images'))
        for s in store_samples:
            image_name = next(i for i in images if i.startswith(f'{s["key"]}_'))
            write_thumbnail(
                os.path.join(zarr_path, 'images', image_name),
                os.path.join(
                    os.path.dirname(os.path.abspath(out_path)),
                    thumbnail_name(out_path, s['key']),
                ),
            )
    if paths['metric_cols']:
        write_metric_values(
            os.path.join(zarr_path, table_path),
            paths['metric_cols'],
            out_path.removesuffix(CONFIG_SUFFIX) + METRIC_VALUES_SUFFIX,
        )

    if study:
        print(
            f'Wrote {out_path} ({len(store_samples)} samples, '
            f'table={table_path}, thumbnails beside it)'
        )
    else:
        print(
            f'Wrote {out_path} (image={paths["image_path"]}, '
            f'segmentations={paths["obs_segmentations_path"]}, '
            f'points={paths["obs_points_path"]}, '
            f'metrics={paths["metric_cols"]}, '
            f'coordinate_system={paths["coordinate_system"]})'
        )

    return out_path


# Function to parse command-line arguments
def parse_args():
    """
    Parse command-line arguments for spatial config generation.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments (zarr, spatial_root, serve_base, out, image,
      segmentations, obs_type, sample).
    """
    parser = argparse.ArgumentParser(
        description='Generate a Vitessce config JSON for a SpatialData store.'
    )
    parser.add_argument(
        '--zarr', required=True, help='Path to SpatialData .zarr store.'
    )
    parser.add_argument(
        '--spatial-root',
        default=os.environ.get('SPATIAL_DATA', 'data/spatial'),
        help='Directory gateway serves at --serve-base (SPATIAL_DATA), '
        'holding store in any subfolder and default config folder '
        'vitessce_configs/ (default: $SPATIAL_DATA or data/spatial).',
    )
    parser.add_argument(
        '--serve-base',
        default='/spatial-data',
        help='URL prefix under which spatial root is served by gateway.',
    )
    parser.add_argument(
        '--out',
        default=None,
        help='Output config path (default: '
        '<spatial root>/vitessce_configs/<store>.vitessce.json). Config without '
        'metrics is written alongside, as <name>.nometrics.vitessce.json.',
    )
    parser.add_argument(
        '--image', default=None, help='Image element name override.'
    )
    parser.add_argument(
        '--segmentations',
        default=None,
        help='Labels or shapes element name override.',
    )
    parser.add_argument(
        '--obs-type',
        default='cell',
        help='Observation label used across views (default: cell).',
    )
    parser.add_argument(
        '--sample',
        default=None,
        help='Sample key of multi-sample store (element name prefix).',
    )

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    zarr_path = os.path.normpath(args.zarr)
    spatial_root = os.path.normpath(args.spatial_root)
    # Store URL keeps store's folder under root, e.g. /spatial-data/xenium/
    store_dir = os.path.relpath(os.path.dirname(zarr_path), spatial_root)
    if store_dir.split(os.sep)[0] == '..':
        raise SystemExit(f'{zarr_path} is not under {spatial_root}')
    serve_base = args.serve_base.rstrip('/')
    if store_dir != '.':
        serve_base = f'{serve_base}/{store_dir}'
    if args.out:
        out = args.out
    else:
        # One config folder for every store, so shared links never name
        # store's folder
        stem = os.path.basename(os.path.splitext(zarr_path)[0])
        if args.sample:
            stem = f'{stem}_{args.sample}'
        out = os.path.join(
            spatial_root, 'vitessce_configs', stem + CONFIG_SUFFIX
        )
    # Both written, so gateway can switch Metric layer at start-up
    for config_out, metrics in (
        (out, True),
        (out.removesuffix(CONFIG_SUFFIX) + NO_METRICS_SUFFIX, False),
    ):
        generate_config(
            zarr_path,
            serve_base,
            config_out,
            image=args.image,
            segmentations=args.segmentations,
            obs_type=args.obs_type,
            metrics=metrics,
            sample=args.sample,
        )
