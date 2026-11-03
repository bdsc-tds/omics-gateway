"""
Script to convert Xenium Ranger output bundle into SpatialData .zarr store for
Vitessce spatial viewer, either as single-sample store or as one sample of
multi-sample store.

Memory is binding constraint: full-resolution morphology image spans tens of
thousands of pixels per side, so naive `SpatialData.write()` under dask's
default threaded scheduler exhausts 16 GB workstation. This script forces
single-threaded scheduler and small chunks, and writes element by element, so
peak usage stays bounded and failure is partial rather than total.

Transcripts below quality 20 (qv) and control probes are dropped, so
Transcript layer shows only molecules counted in Xenium Ranger's cell counts,
which drive gene colouring.

Single-sample store (default) keeps every cell and Xenium's element names,
with Xenium Ranger's UMAP and optional --cell-types. --metadata writes dataset
metadata into table, from which generate_datasets_tsv.py builds store's
datasets.tsv row (file format in its load_metadata()).

Study sample (--sample-id, --key and --joint) has layout of
convert_visium_hd.py: each sample gets own coordinate system (named by --key)
and elements prefixed by it: '<key>_morphology_focus',
'<key>_cell_boundaries', '<key>_nucleus_boundaries', '<key>_table' and
'<key>_transcripts'; cell ids are prefixed too. Store is created on first
sample and extended afterwards; converting sample again replaces its
elements. 'tables/table' is left free for study-wide merged table
(merge_study_tables.py), which also takes study's metadata. Only cells of
study's joint analysis are kept, read from CSV with 'sample', 'cell_id',
'umap_1', 'umap_2' (joint UMAP, stored as X_umap) and 'sample_umap_1',
'sample_umap_2' (sample's own, X_umap_sample) columns; every further column
becomes cell set list. Sample's UMAP comes from analysis too, as Xenium
Ranger's leaves out some annotated cells.

--rotate turns whole sample clockwise by quarter turns, since 2D viewer cannot
rotate it.

Always run it under cgroup memory cap so runaway process cannot take desktop
down with it:

Usage (single-sample store):
    systemd-run --user --scope -p MemoryMax=5G -p MemorySwapMax=0 \
        conda run -n spatial python data_prep/convert_xenium.py \
            --xenium-dir spatial_sources/xenium_sample \
            --out data/spatial/xenium/xenium_sample.zarr \
            --metadata spatial_sources/xenium_sample.metadata.yaml

Usage (study sample; cluster batch job, one sample per call, chained, since
parallel writes race on root metadata):
    python data_prep/convert_xenium.py \
        --xenium-dir .../xenium_skin_1 \
        --sample-id S1 --key s1 \
        --joint joint_umap.csv \
        --out xenium_skin.zarr
"""

# Import utility modules
import argparse
import os
import time

import dask
import dask.array as da
import geopandas as gpd
import numpy as np
import pandas as pd
import spatialdata_io

# Import sibling modules; data_prep/ is on sys.path when scripts run directly
from generate_datasets_tsv import load_metadata, write_store_metadata
from spatial_common import (
    CENTROIDS_KEY,
    add_genes_detected,
    add_set_columns,
    downcast_int64_obs,
    normalise_string_dtypes,
    peak_rss_gb,
    quarter_turn_matrix,
    read_joint_cells,
    transform_geometry,
    write_elements,
)
from spatialdata import SpatialData
from spatialdata.models import (
    Image2DModel,
    PointsModel,
    ShapesModel,
    TableModel,
)
from spatialdata.transformations import (
    Identity,
    get_transformation,
    set_transformation,
)
from vitessce.data_utils import (
    sdata_morton_sort_points,
    sdata_points_modify_row_group_size,
)

# Single-threaded: dask's default scheduler runs one task per core, each
# materialising image chunk, so peak memory scales with core count
dask.config.set(scheduler='synchronous')

# Written singly, lightest first, so failure leaves partial store; shapes
# precede table, which annotates them by region name
ELEMENT_ORDER = [
    'cell_boundaries',
    'nucleus_boundaries',
    'table',
    'cell_labels',
    'morphology_focus',
    'transcripts',
]

# Only columns viewer reads. Xenium carries nine more per transcript, and at
# millions of rows dropping them is what keeps sort and store affordable
TRANSCRIPT_COLUMNS = ['x', 'y', 'z', 'feature_name']

# Xenium Ranger's cell counts, behind gene colouring, keep only calls at or
# above this quality (checked: transcript_counts sum matches exactly)
MIN_TRANSCRIPT_QV = 20

# Row group small enough that browser can fetch spatial tile as byte range
# instead of pulling whole file
TRANSCRIPT_ROW_GROUP_SIZE = 50_000


# Function to attach cell type labels to table
def add_cell_types(table, csv_path):
    """
    Attach externally curated cell type labels to SpatialData table.

    Cells missing from annotation file are labelled 'Unassigned' rather than
    left as NaN, since Vitessce's obsSets view cannot render missing values.

    Parameters:
    -----------
    table: anndata.AnnData
      Table element of SpatialData object, indexed by cell id.
    csv_path: str
      Path to .csv with 'cell_id' and 'group' columns.

    Returns:
    --------
    n_unassigned: int
      Number of cells without annotation.
    """
    types = pd.read_csv(csv_path).set_index('cell_id')['group']
    mapped = table.obs['cell_id'].map(types).fillna('Unassigned')
    table.obs['cell_type'] = pd.Categorical(mapped)

    return int((mapped == 'Unassigned').sum())


# Function to attach precomputed UMAP embedding to table
def add_umap(table, csv_path):
    """
    Attach Xenium Ranger UMAP projection to SpatialData table.

    Parameters:
    -----------
    table: anndata.AnnData
      Table element of SpatialData object, indexed by cell id.
    csv_path: str
      Path to Xenium Ranger's umap projection.csv ('Barcode', 'UMAP-1',
      'UMAP-2').

    Returns:
    --------
    n_missing: int
      Number of cells with no embedding coordinates, filled with zeros.
    """
    umap = pd.read_csv(csv_path).set_index('Barcode')
    aligned = umap.reindex(table.obs['cell_id'])
    n_missing = int(aligned.isna().any(axis=1).sum())
    table.obsm['X_umap'] = aligned.fillna(0.0).to_numpy()

    return n_missing


# Function to add cell centroids in viewer's rendered coordinates
def add_global_centroids(sdata, region, key=CENTROIDS_KEY):
    """
    Copy table's cell centroids into obsm, scaled to region's coordinate system.

    Vitessce does lasso selection and cross-view hover by point-in-polygon on
    obsLocations. Its AnnData loader applies no coordinate transform, while
    shapes loader bakes element's transform into polygon coordinates, so
    centroids must be stored already transformed. Xenium writes them in
    microns, so region's own scale is read from store rather than hardcoded.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Object holding table and shapes element table annotates.
    region: str
      Name of that shapes element (e.g. 'cell_boundaries').
    key: str
      obsm key to write transformed centroids under.

    Returns:
    --------
    scale: list of float
      x and y scale factors applied, for logging.
    """
    table = sdata.tables['table']
    affine = get_transformation(sdata.shapes[region]).to_affine_matrix(
        input_axes=('x', 'y'), output_axes=('x', 'y')
    )
    centroids = np.asarray(table.obsm['spatial'], dtype='float64')
    homogeneous = np.column_stack([
        centroids,
        np.ones(len(centroids), dtype='float64'),
    ])
    table.obsm[key] = (homogeneous @ affine.T)[:, :2]

    return [float(affine[0, 0]), float(affine[1, 1])]


# Function to point table at segmentation element that was written
def retarget_table_region(sdata, region):
    """
    Repoint table's annotated region at element held by store.

    Xenium reader annotates table against 'cell_labels', but rasterised label
    pyramid is skipped when polygon boundaries are used. Vitessce joins table to
    segmentations through region name, so dangling reference leaves every view
    unlinked.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Object whose table should be repointed.
    region: str
      Name of element annotated by table.

    Returns:
    --------
    None
    """
    table = sdata.tables['table']
    table.obs['region'] = pd.Categorical([region] * table.n_obs)
    TableModel.parse(
        table,
        region=region,
        region_key='region',
        instance_key='cell_id',
        overwrite_metadata=True,
    )


# Function to index nucleus polygons by cell they belong to
def reindex_nucleus_boundaries(sdata):
    """
    Replace nucleus boundaries' positional index with their cell id.

    Xenium reader leaves nuclei on meaningless integer range with cell id in
    ordinary column, so parquet records '__index_level_0__' as index column.
    Vitessce reads that metadata for its obs index whenever file def carries no
    tablePath, which is how extra segmentations are served, so ids there must
    mean something. Cells are already indexed by cell id, so this mirrors them.

    Index is deliberately left non-unique: cells carrying several nucleus
    polygons repeat their id, and dropping those polygons would lose real data.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Object whose 'nucleus_boundaries' element is reindexed in place.

    Returns:
    --------
    n_duplicated: int
      Number of nuclei whose cell id is shared with another nucleus.
    """
    nuclei = sdata.shapes['nucleus_boundaries']

    # Assigned in place: set_index returns new frame, losing transform in .attrs
    nuclei.index = pd.Index(nuclei['cell_id'])
    del nuclei['cell_id']

    return int(nuclei.index.duplicated().sum())


# Function to express global rotation in element's own coordinates
def element_rotation(element, rotation):
    """
    Conjugate global-frame rotation by element's transform to global.

    Shapes and points stay in microns with their micron-to-pixel scale, so
    their coordinates are rotated by T^-1 R T rather than by R itself.

    Parameters:
    -----------
    element: spatialdata element
      Shapes or points carrying 'global' transformation.
    rotation: numpy.ndarray
      3x3 rotation of global frame, from quarter_turn_matrix.

    Returns:
    --------
    matrix: numpy.ndarray
      3x3 affine matrix acting on element's own (x, y, 1).
    """
    to_global = get_transformation(element).to_affine_matrix(
        input_axes=('x', 'y'), output_axes=('x', 'y')
    )

    return np.linalg.inv(to_global) @ rotation @ to_global


# Function to rotate whole sample by clockwise quarter turns
def rotate_sample(sdata, degrees, image_name='morphology_focus'):
    """
    Rotate image, shapes, transcripts and table centroids clockwise in place.

    Data are rotated physically rather than given rotation transform, so
    viewer and every downstream step see ordinary upright sample. Must run
    before centroids are copied to global frame and before transcripts are
    Morton-sorted, both of which read rotated coordinates.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Freshly read Xenium object; rasterised labels are not supported.
    degrees: int
      Clockwise rotation: 90, 180 or 270.
    image_name: str
      Image element defining global frame's extent.

    Returns:
    --------
    size: tuple of int
      Rotated image width and height in pixels.
    """
    image = sdata.images[image_name]
    if get_transformation(image) != Identity():
        raise ValueError(f'{image_name} must sit on identity transform.')
    base = image['scale0']['image']
    _, height, width = base.shape
    rotation = quarter_turn_matrix(degrees, width, height)

    # Pyramid rebuilt from rotated full resolution with original level count
    rotated = da.rot90(base.data, k=-degrees // 90, axes=(1, 2))
    n_levels = len(image.children)
    sdata.images[image_name] = Image2DModel.parse(
        rotated.rechunk((1, 2048, 2048)),
        dims=('c', 'y', 'x'),
        c_coords=list(base.coords['c'].values),
        scale_factors=[2] * (n_levels - 1),
        chunks=(1, 2048, 2048),
        transformations={'global': Identity()},
    )

    for name in sdata.shapes:
        shapes = sdata.shapes[name]
        shapes.geometry = transform_geometry(
            shapes.geometry, element_rotation(shapes, rotation)
        )

    if 'transcripts' in sdata.points:
        points = sdata.points['transcripts']
        transformations = get_transformation(points, get_all=True)
        m = element_rotation(points, rotation)
        x, y = points['x'], points['y']
        rotated_points = points.assign(
            x=m[0, 0] * x + m[0, 1] * y + m[0, 2],
            y=m[1, 0] * x + m[1, 1] * y + m[1, 2],
        )
        sdata.points['transcripts'] = parse_points_keeping_transform(
            rotated_points, transformations
        )

    # Table centroids share cell outlines' micron frame
    table = sdata.tables['table']
    m = element_rotation(sdata.shapes['cell_boundaries'], rotation)
    centroids = np.asarray(table.obsm['spatial'], dtype='float64')
    table.obsm['spatial'] = centroids @ m[:2, :2].T + m[:2, 2]

    return (height, width) if degrees in (90, 270) else (width, height)


# Function to re-parse points element without losing its coordinate transform
def parse_points_keeping_transform(points, transformations):
    """
    Parse points element and restore coordinate transform afterwards.

    PointsModel.parse refuses transformations passed alongside element that
    already carries one, and applies identity when none is given. Setting it
    after parsing sidesteps both, and identity here would place transcripts in
    microns while image and shapes are in pixels.

    Parameters:
    -----------
    points: dask.dataframe.DataFrame
      Points to parse.
    transformations: dict
      Coordinate-system name to transformation, as captured before parsing.

    Returns:
    --------
    parsed: dask.dataframe.DataFrame
      Parsed points element carrying original transform.
    """
    parsed = PointsModel.parse(points, feature_key='feature_name')
    set_transformation(parsed, transformations, set_all=True)

    return parsed


# Function to make transcripts spatially queryable by viewer
def prepare_transcripts(sdata, var_names):
    """
    Rewrite transcripts point cloud in form Vitessce can query by region.

    Viewer fetches points for visible rectangle only when store carries
    Morton (Z-order) code per point and rows are sorted by it. It also needs
    integer index into table's var names per point, since it colours points by
    gene without reading gene strings. Dictionary-encoded columns must be
    rightmost, which is why columns are reordered last.

    Parameters:
    -----------
    sdata: spatialdata.SpatialData
      Object whose 'transcripts' element is rewritten in place.
    var_names: pandas.Index
      Gene names of annotating table, defining feature index per point.

    Returns:
    --------
    n_dropped: int
      Number of control-probe detections removed.
    n_low_qv: int
      Number of gene transcripts removed for quality below MIN_TRANSCRIPT_QV.
    """
    points = sdata.points['transcripts']

    # Captured because morton sort re-parses element without them, which would
    # silently reset micron-to-pixel scale to identity
    transformations = get_transformation(points, get_all=True)

    # Control probes are absent from var names, so they cannot carry feature
    # index; dropping beats writing them with sentinel code
    is_gene = points['is_gene']
    # Low-quality calls are not counted, so layer would show uncounted molecules
    passes_qv = points['qv'] >= MIN_TRANSCRIPT_QV
    n_dropped, n_low_qv = dask.compute(
        (~is_gene).sum(), (is_gene & ~passes_qv).sum()
    )
    points = points[is_gene & passes_qv][TRANSCRIPT_COLUMNS]

    # Map through categories rather than per-row lookup: millions of lookups
    # against list is minutes, this is one pass over ~500 categories
    points['feature_name'] = points['feature_name'].cat.as_known()
    lookup = pd.Series(
        [
            var_names.get_loc(name) if name in var_names else -1
            for name in points['feature_name'].cat.categories
        ],
        dtype='int32',
    )
    points['feature_name_codes'] = (
        points['feature_name']
        .cat.codes.map(lookup, meta=('feature_name_codes', 'int32'))
        .astype('int32')
    )

    sdata.points['transcripts'] = parse_points_keeping_transform(
        points, transformations
    )
    sdata_morton_sort_points(sdata, 'transcripts')

    # Reordered after sort because sort appends its own numeric columns
    sorted_points = sdata.points['transcripts']
    ordered = [
        name
        for name in sorted_points.columns
        if not isinstance(sorted_points[name].dtype, pd.CategoricalDtype)
    ]
    ordered += [name for name in sorted_points.columns if name not in ordered]
    sdata.points['transcripts'] = parse_points_keeping_transform(
        sorted_points[ordered], transformations
    )

    return int(n_dropped), int(n_low_qv)


# Function to read Xenium bundle into SpatialData object
def read_xenium(xenium_dir, transcripts, cells_labels, nucleus_boundaries):
    """
    Read Xenium Ranger bundle, skipping elements not used by viewer.

    4 GB morphology MIP is always skipped in favour of multi-channel
    morphology_focus images, and nucleus labels are skipped because nuclei are
    drawn from polygon boundaries.

    Parameters:
    -----------
    xenium_dir: str
      Path to Xenium Ranger output directory.
    transcripts: bool
      Whether to include transcripts point cloud.
    cells_labels: bool
      Whether to rasterise cell segmentation masks. Polygon boundaries are
      preferred; this is fallback when viewer cannot render them.
    nucleus_boundaries: bool
      Whether to include nucleus polygon boundaries as second segmentation.

    Returns:
    --------
    sdata: spatialdata.SpatialData
      Lazily loaded SpatialData object.
    """
    return spatialdata_io.xenium(
        xenium_dir,
        cells_boundaries=True,
        nucleus_boundaries=nucleus_boundaries,
        cells_labels=cells_labels,
        nucleus_labels=False,
        transcripts=transcripts,
        morphology_mip=False,
        morphology_focus=True,
        aligned_images=False,
        cells_table=True,
        n_jobs=1,
        image_models_kwargs={'chunks': (1, 2048, 2048)},
        labels_models_kwargs={'chunks': (2048, 2048)},
    )


# Function to write SpatialData object element by element
def write_incrementally(source, out_path, skip=()):
    """
    Write SpatialData store element by element, reporting peak memory.

    Writing whole object in one call materialises several large pyramids
    concurrently; writing per element bounds peak. Empty store is created first
    because write_element requires object to be backed.

    Parameters:
    -----------
    source: spatialdata.SpatialData
      Lazily loaded object whose elements are copied into store.
    out_path: str
      Destination .zarr store path.
    skip: tuple of str
      Element names to leave unwritten, for elements needing preparation that
      must happen after heavier elements have been written and released.

    Returns:
    --------
    backed: spatialdata.SpatialData
      Store-backed object, ready for further write_element calls.
    """
    # gen_elements yields (element_type, name, element)
    names = [name for _, name, _ in source.gen_elements()]
    ordered = [n for n in ELEMENT_ORDER if n in names]
    ordered += [n for n in names if n not in ordered]
    ordered = [n for n in ordered if n not in skip]

    # write_element only works on backed object, so establish store first
    backed = SpatialData()
    backed.write(out_path, overwrite=True)

    for name in ordered:
        start = time.time()
        backed[name] = source[name]
        backed.write_element(name, overwrite=True)
        print(
            f'  wrote {name} in {time.time() - start:.1f}s '
            f'(peak RSS {peak_rss_gb():.2f} GB)',
            flush=True,
        )

    return backed


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
def build_sample_table(sdata, cells, key):
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

    add_set_columns(table.obs, cells, ids)
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
    add_genes_detected(table)
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
    Parse command-line arguments for Xenium conversion.

    Returns:
    --------
    args: argparse.Namespace
      Parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description='Convert a Xenium bundle to a SpatialData .zarr store, or '
        'add it to a multi-sample store.'
    )
    parser.add_argument(
        '--xenium-dir', required=True, help='Xenium Ranger output directory.'
    )
    parser.add_argument(
        '--out', required=True, help='Destination .zarr store path.'
    )
    parser.add_argument(
        '--sample-id',
        default=None,
        help='Study sample: sample id in joint analysis CSV.',
    )
    parser.add_argument(
        '--key',
        default=None,
        help='Study sample: element prefix and coordinate system.',
    )
    parser.add_argument(
        '--joint',
        default=None,
        help='Study sample: joint analysis CSV (cells, UMAP, sets).',
    )
    parser.add_argument(
        '--cell-types',
        default=None,
        help="Optional .csv with 'cell_id' and 'group' columns.",
    )
    parser.add_argument(
        '--no-transcripts',
        dest='transcripts',
        action='store_false',
        help='Skip the transcripts point cloud (much the slowest element).',
    )
    parser.add_argument(
        '--no-nucleus-boundaries',
        dest='nucleus_boundaries',
        action='store_false',
        help='Skip nucleus polygon boundaries.',
    )
    parser.add_argument(
        '--cells-labels',
        action='store_true',
        help='Rasterise cell masks instead of using polygon boundaries.',
    )
    parser.add_argument(
        '--metadata',
        default=None,
        help='Optional YAML of dataset metadata for datasets.tsv '
        '(see load_metadata() in generate_datasets_tsv.py).',
    )
    parser.add_argument(
        '--rotate',
        type=int,
        default=0,
        choices=[0, 90, 180, 270],
        help='Rotate the sample clockwise by this many degrees.',
    )
    args = parser.parse_args()
    study = [args.sample_id, args.key, args.joint]
    if any(study) and not all(study):
        parser.error('--sample-id, --key and --joint go together.')
    single_only = {
        '--cell-types': args.cell_types,
        '--no-transcripts': not args.transcripts,
        '--no-nucleus-boundaries': not args.nucleus_boundaries,
        '--cells-labels': args.cells_labels,
        '--metadata': args.metadata,
    }
    given = [flag for flag, value in single_only.items() if value]
    if args.key and given:
        parser.error(f'{", ".join(given)}: single-sample stores only.')

    return args


# Function to convert bundle into single-sample store
def convert_single_store(args):
    """
    Convert whole bundle into single-sample store under Xenium's own element
    names, with optional cell types and metadata.

    Parameters:
    -----------
    args: argparse.Namespace
      Parsed arguments, without study sample ones.

    Returns:
    --------
    None
    """
    metadata = load_metadata(args.metadata) if args.metadata else None

    # Elements are dask-backed, so cheap operation
    start = time.time()
    sdata = read_xenium(
        args.xenium_dir,
        args.transcripts,
        args.cells_labels,
        args.nucleus_boundaries,
    )
    print(f'read in {time.time() - start:.1f}s', flush=True)
    print(sdata, flush=True)

    if args.rotate:
        if args.cells_labels:
            raise ValueError('--rotate does not support --cells-labels.')
        size = rotate_sample(sdata, args.rotate)
        print(
            f'rotated {args.rotate} degrees clockwise ({size[0]}x{size[1]} px)',
            flush=True,
        )

    if 'nucleus_boundaries' in sdata.shapes:
        n = reindex_nucleus_boundaries(sdata)
        print(f'nuclei reindexed by cell id ({n} duplicate ids)', flush=True)

    # Enrich table before writing it: store is written once, per element
    table = sdata.tables['table']
    if not args.cells_labels:
        retarget_table_region(sdata, 'cell_boundaries')
        print('table region repointed to cell_boundaries', flush=True)
    if args.cell_types:
        n = add_cell_types(table, args.cell_types)
        print(f'cell types attached ({n} cells unassigned)', flush=True)

    # Xenium Ranger's own UMAP, so viewer doesn't need another embedding
    umap_csv = os.path.join(
        args.xenium_dir,
        'analysis',
        'umap',
        'gene_expression_2_components',
        'projection.csv',
    )
    if os.path.isfile(umap_csv):
        n = add_umap(table, umap_csv)
        print(f'UMAP attached ({n} cells without coordinates)', flush=True)

    # Lasso and cross-view hover test centroids, not polygons; label
    # segmentations have no shapes element to read scale from
    if not args.cells_labels:
        scale = add_global_centroids(sdata, 'cell_boundaries')
        print(f'global centroids written (scale {scale})', flush=True)

    add_genes_detected(table)

    # Must run last, after every column steps above may add
    converted = normalise_string_dtypes(table)
    if converted:
        print(f'string dtypes normalised: {", ".join(converted)}', flush=True)
    converted = downcast_int64_obs(table)
    if converted:
        print(f'int64 obs downcast: {", ".join(converted)}', flush=True)

    # Peak memory is reported so cap can be sized from real run. Transcripts are
    # held back so their sort does not run alongside image pyramids
    backed = write_incrementally(sdata, args.out, skip=('transcripts',))

    if args.transcripts:
        step = time.time()
        n, n_low_qv = prepare_transcripts(sdata, table.var.index)
        print(
            f'transcripts prepared in {time.time() - step:.1f}s '
            f'({n} control-probe detections and {n_low_qv} below qv '
            f'{MIN_TRANSCRIPT_QV} dropped)',
            flush=True,
        )
        step = time.time()
        backed['transcripts'] = sdata['transcripts']
        backed.write_element('transcripts', overwrite=True)
        print(
            f'  wrote transcripts in {time.time() - step:.1f}s '
            f'(peak RSS {peak_rss_gb():.2f} GB)',
            flush=True,
        )
        sdata_points_modify_row_group_size(
            backed, 'transcripts', TRANSCRIPT_ROW_GROUP_SIZE
        )

    if metadata is not None:
        keys = write_store_metadata(args.out, metadata)
        print(f'metadata written: {", ".join(keys)}', flush=True)

    backed.write_consolidated_metadata()
    print(
        f'done in {time.time() - start:.1f}s, peak RSS '
        f'{peak_rss_gb():.2f} GB -> {args.out}',
        flush=True,
    )


# Function to add bundle as one sample of multi-sample store
def convert_study_sample(args):
    """
    Add joint analysis cells of bundle to multi-sample store, under sample's
    key.

    Parameters:
    -----------
    args: argparse.Namespace
      Parsed arguments, with --sample-id, --key and --joint.

    Returns:
    --------
    None
    """
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

    table = build_sample_table(sdata, cells, args.key)
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


if __name__ == '__main__':
    args = parse_args()
    # Fail before expensive read rather than on silently empty result
    if not os.path.isdir(args.xenium_dir):
        raise FileNotFoundError(
            f'Xenium directory {os.path.abspath(args.xenium_dir)} does not exist.'
        )
    if args.key:
        convert_study_sample(args)
    else:
        convert_single_store(args)
