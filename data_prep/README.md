# Data preparation

The gateway does not process data: it lists the datasets described in a table and opens each one in a viewer. The scripts in this folder prepare those files from analysis outputs. They run offline, before the data is copied to the server, and the gateway never calls them. They were written for our team's pipeline, but they only depend on the input formats described below, so they work for anyone producing the same files.

## Overview

```
.h5ad files from a single-cell pipeline ───────────────────────────┐
                                                                   ├─> generate_datasets_tsv.py ─> datasets.tsv
Xenium Ranger output ─> convert_xenium.py ─> <sample>.zarr ────────┤
Space Ranger output ─> convert_visium_hd.py ─> <study>.zarr        │
                                                   │               │
                                                   └─> generate_spatial_config.py ─> viewer configs
QC figures ─> downscale_qc_figures.py (optional) ─> build_qc_thumbnails.py ─> thumbnails
```

| Script | Environment | Produces |
| --- | --- | --- |
| `generate_datasets_tsv.py` | `omics-gateway` | `datasets.tsv`, the table of datasets shown in the browser |
| `convert_xenium.py` | `spatial` | a SpatialData `.zarr` store, from Xenium Ranger output |
| `convert_visium_hd.py` | `spatial` | one sample of a multi-sample SpatialData store, from Space Ranger output |
| `write_store_metadata.py` | `spatial` | the dataset's metadata inside an existing store |
| `generate_spatial_config.py` | `spatial` | the viewer configs for a store |
| `downscale_qc_figures.py` | `omics-gateway` | a lower-resolution copy of the QC figures |
| `build_qc_thumbnails.py` | `omics-gateway` | the QC report thumbnails |

Where these files end up on the server is described under "Data layout" in the main README.

## Terms used here

- **AnnData (`.h5ad`)**: the standard file format for single-cell data in Python. A file holds a table of cells by genes, per-cell annotations, and a free-form metadata dictionary called **`uns`**. The gateway reads each dataset's title, authors, assay and similar details from `uns`.
- **Zarr**: a format for large arrays, split into compressed chunks and stored as an ordinary folder: one file per chunk, plus small JSON files (`zarr.json`) describing the layout. A browser can download just the chunks it needs, over plain HTTP, instead of the whole dataset.
- **SpatialData store (`.zarr`)**: a Zarr folder organised by the [SpatialData](https://spatialdata.scverse.org/) convention for spatial omics. Images are stored as resolution pyramids, so zoomed-out views load small versions; cell outlines and transcript positions are [Parquet](https://parquet.apache.org/) tables; gene expression and cell metadata are an AnnData table, whose `uns` plays the same role as in an `.h5ad` file.
- **Viewer config**: a JSON file telling the spatial viewer, [Vitessce](https://vitessce.io), which parts of a store to show and how to lay out its panels.

## Environments

The scripts use two conda environments, both created from files in this repository:

- **`omics-gateway`**, the environment the gateway itself runs in, created by `deploy/setup.sh` (see the main README).
- **`spatial`**, for the spatial scripts, created once with:

    ```bash
    conda env create -f data_prep/spatial_env.yaml
    ```

Keep the two separate: the spatial packages are large and pin their own versions, and the server does not need them.

## Adding a single-cell dataset

1. Make sure the `.h5ad` file carries the metadata keys listed under [Dataset table](#dataset-table) in its `uns`. Missing keys leave the matching columns empty.
2. Copy it into the data directory, in a subfolder if you like.
3. Regenerate the dataset table:

    ```bash
    conda run -n omics-gateway python data_prep/generate_datasets_tsv.py \
        --data-dir data \
        --output datasets.tsv
    ```

`datasets.tsv` is a plain tab-separated file, so it can also be written or corrected by hand; its columns are described in the main README.

## Adding a Xenium sample

A Xenium sample becomes one SpatialData store plus its viewer configs. You need:

- the **Xenium Ranger output folder** for the sample (the files it uses are listed under [What the conversion reads](#what-the-conversion-reads));
- optionally, a **cell-type file**: a CSV with a `cell_id` column (Xenium's cell IDs) and a `group` column (the cell type). Cells missing from it are labelled `Unassigned`;
- a **metadata file**, described in step 1.

Keep these inputs in `spatial_sources/` at the repository root, which git ignores. Never put them under `data/`: the gateway serves every file there to anyone who knows its path.

### 1. Write the metadata file

This YAML file holds the details shown in the dataset browser, under the same keys as an `.h5ad` file's `uns` (see [Dataset table](#dataset-table)). Values are strings or lists of strings; the cell and gene counts can be left out, as they are read from the data:

```yaml
dataset_name_short: D1903482ppAD
dataset_name: Xenium Skin (D1903482)
article_title: "Xenium in situ spatial transcriptomics of atopic dermatitis skin, hSkin_100g panel"
assay: Xenium
disease: Atopic Dermatitis
tissue: Skin
patients: 1
article_date: 2025
article_authors: A. Thiebaut
```

`dataset_name_short` is required for the sample to appear in the browser; it also names the sample's QC folder. An unknown key stops the conversion before it starts, which catches typos.

### 2. Convert the output folder

```bash
systemd-run --user --scope -p MemoryMax=5G -p MemorySwapMax=0 \
    conda run -n spatial python data_prep/convert_xenium.py \
        --xenium-dir spatial_sources/<sample> \
        --out data/<sample>.zarr \
        --cell-types spatial_sources/<sample>_cell_types.csv \
        --metadata spatial_sources/<sample>.metadata.yaml
```

The `systemd-run` prefix caps the conversion's memory at 5 GB, so that if it ever grows out of bounds, only the conversion is stopped and not the rest of the machine. Always use it, or an equivalent limit on systems without systemd: writing the full-resolution image without precautions once exhausted a 16 GB workstation. The script itself keeps the peak at about 3 GB, and a sample of 39,000 cells takes about two minutes on a 20-core machine.

Options:

- `--no-transcripts` skips the transcript positions, by far the slowest part; the viewer then has no Transcript layer.
- `--no-nucleus-boundaries` skips the nucleus outlines; the viewer then has no Nucleus layer.
- `--rotate 90`, `180` or `270` turns the sample clockwise by that many degrees, for samples whose orientation on the slide is inconvenient to read. The viewer cannot rotate a sample itself, so this is the only way to change its orientation. It cannot be combined with `--cells-labels`.

What the script does to the data is described under [What the conversion does](#what-the-conversion-does).

### 3. Generate the viewer configs

```bash
conda run -n spatial python data_prep/generate_spatial_config.py \
    --zarr data/<sample>.zarr
```

This writes three files into `data/vitessce_configs/`:

- `<sample>.vitessce.json`, the viewer config with a Metric layer, which colours cells by measurements such as cell area or transcript count;
- `<sample>.nometrics.vitessce.json`, the same config without it;
- `<sample>.metrics.json`, the per-cell measurements shown in cell tooltips.

The gateway's `SPATIAL_METRICS` setting picks which of the two configs the browser links to.

### 4. Regenerate the dataset table

Run `generate_datasets_tsv.py` as in [Adding a single-cell dataset](#adding-a-single-cell-dataset). The store gets its own row, after the `.h5ad` files.

### 5. Check it in the browser

Start the gateway (see "Running the gateway" in the main README), open the dataset browser and click the sample's **Explore** button. The image can stay black for about ten seconds while it streams in.

### Correcting metadata later

To change a store's metadata without converting it again, edit the YAML file and run:

```bash
conda run -n spatial python data_prep/write_store_metadata.py \
    --zarr data/<sample>.zarr \
    --metadata spatial_sources/<sample>.metadata.yaml
```

Then regenerate the dataset table.

## Adding a Visium HD study

A Visium HD study becomes one SpatialData store holding all its samples, plus one viewer config per sample. Each sample is added by a separate run of `convert_visium_hd.py`. You need, per sample:

- the **Space Ranger output folder** (`outs`, Space Ranger 4 or later, with its cell segmentation in `segmented_outputs/`);
- the **H&E image** given to Space Ranger, as a pyramidal OME-TIFF;
- an **RCTD result**: a CSV with `Barcode` (Space Ranger's cell ID), `spot_class` and `first_type` columns;
- the **joint analysis export**: a CSV with one row per cell kept after quality control, and the columns `cell_id` (Space Ranger's cell ID), `sample` (the sample's ID in the joint analysis), `cluster`, `umap_1` and `umap_2`. Only the cells listed there are kept.

The H&E images are large (several GB each at full resolution), so run the conversion where memory is plentiful, such as a cluster job with 16 GB or more. It takes a few minutes per sample and stays below 2.5 GB. Run the samples one after another, never in parallel, as they write into the same store:

```bash
conda run -n spatial python data_prep/convert_visium_hd.py \
    --outs <space ranger outs> \
    --he <he image>.ome.tif \
    --sample-id <ID in the joint analysis> \
    --key <short key, e.g. pso1> \
    --rctd <rctd result>.csv \
    --merged-umap <joint analysis export>.csv \
    --out data/<study>.zarr
```

Each sample's elements are named after its key (`<key>_he`, `<key>_cell_boundaries`, `<key>_nucleus_boundaries`, `<key>_table`), and its cell IDs are prefixed with it. The table holds Space Ranger's counts, two cell-type columns (`cell_type`, RCTD's call with rejected cells as `Unassigned`; `cell_type_best_guess`, RCTD's top type for every cell), the joint analysis cluster and two UMAPs (`X_umap` from the joint analysis, `X_umap_sample` from Space Ranger). The H&E is stored at half its scan resolution, as three channels the viewer shows in true colour with a slider each.

Then generate each sample's viewer configs:

```bash
conda run -n spatial python data_prep/generate_spatial_config.py \
    --zarr data/<study>.zarr --sample <key>
```

They are written as `data/vitessce_configs/<study>_<key>.vitessce.json` and `.nometrics.vitessce.json`.

## Reference

### Dataset table

`generate_datasets_tsv.py` searches the data directory, subfolders included, for `.h5ad` files and SpatialData stores, and writes one row per dataset from the `uns` metadata of its cell table:

| Column | `uns` key |
| --- | --- |
| `dataset_id` | `dataset_name_short` |
| `name` | `dataset_name` (the file name if missing) |
| `description` | `article_title` |
| `assay`, `disease`, `tissue`, `sex`, `patients` | same names |
| `cell_count`, `gene_count` | same names, or the table's size when missing |
| `year` | the first four-digit year in `article_date` |
| `authors`, `journal`, `doi` | `article_authors`, `article_journal`, `article_doi` |
| `default_color` | same name |

Values are cleaned on the way: `healthy` is removed from `disease`, NA values from `tissue` and `sex`, and fields holding several values are sorted with NA values last. Rows come in this order: `.h5ad` files, then the merged meta-analysis file if one is given, then the stores. A store without `dataset_name_short` is skipped, which keeps reference or scratch stores under `data/` out of the browser.

The script reads only these metadata keys and the size of each table, never the tables themselves, so it stays light even for very large files.

Our team also publishes a merged meta-analysis file, whose row can have fields replaced from a YAML file (`meta_analysis_config.yaml` here):

```bash
conda run -n omics-gateway python data_prep/generate_datasets_tsv.py \
    --data-dir data \
    --output datasets.tsv \
    --merged-file data/meta_analysis_all_final_label_transfer_swapped.h5ad \
    --merged-config data_prep/meta_analysis_config.yaml
```

### What the conversion reads

| Xenium Ranger file | Becomes |
| --- | --- |
| `cell_feature_matrix` | gene expression, used for gene colouring, the dot plot and the violin plot |
| `cells.parquet` | the cell table's metadata: centroids, areas and counts, used for lasso selection, hover links between views and the Metric layer |
| `cell_boundaries.parquet` | cell outlines (Cell and Metric layers) |
| `nucleus_boundaries.parquet` | nucleus outlines (Nucleus layer) |
| `transcripts.parquet` | transcript positions (Transcript layer) |
| `morphology_focus/` | the microscopy image |
| `analysis/umap/` | the UMAP embedding |
| `experiment.xenium` | pixel size and run metadata |

### What the conversion does

`convert_xenium.py` reads the output folder with [spatialdata-io](https://github.com/scverse/spatialdata-io), then:

1. **Rotates the sample** if `--rotate` is given: the image pixels themselves are turned, and the cell and nucleus outlines, transcripts and cell centroids are moved with them, so the rest of the pipeline sees an ordinary upright sample. The zoomed-out image levels are rebuilt from the rotated full-resolution image, so they can differ slightly from Xenium Ranger's own.
2. **Adds annotations**: the cell types from the CSV, and Xenium Ranger's own UMAP, so the viewer needs no embedding of its own.
3. **Adapts the data to the viewer.** Several details of a freshly read sample stop Vitessce from showing it, without any error message, so the script links the cell table to the cell outlines, converts text columns to a format Vitessce can read, fills missing annotations, indexes nucleus outlines by cell, and narrows 64-bit integer columns.
4. **Filters and prepares the transcripts.** Control-probe detections are dropped, and so are transcripts with a quality score (`qv`) below 20. Xenium Ranger's own gene counts per cell, which drive gene colouring, use exactly the transcripts at or above 20, so the Transcript layer shows the same molecules the counts include. Only each transcript's position and gene are kept. The rows are then sorted by position and split into blocks of 50,000, so the browser fetches only the transcripts in view.
5. **Writes the store piece by piece**, single-threaded, which keeps memory use bounded and leaves a partial store rather than nothing if something fails.
6. **Writes the metadata** from `--metadata`, as `write_store_metadata.py` would.

## QC reports

The folder layout the gateway expects is described in the main README.

Our pipeline exports figures at 600 dpi, far more than a screen can show, which makes reports slow to load. `downscale_qc_figures.py` writes a copy resampled to 150 dpi into a separate folder, leaving the originals untouched:

```bash
conda run -n omics-gateway python data_prep/downscale_qc_figures.py \
    --qc-data analysis_qc \
    --output analysis_qc_150dpi \
    --dpi 150
```

Check the result, then swap the two folders by hand.

`build_qc_thumbnails.py` builds the thumbnails shown in each report:

```bash
conda run -n omics-gateway python data_prep/build_qc_thumbnails.py \
    --qc-data analysis_qc
```

Run it after every change to the QC figures. Otherwise the gateway builds each report's thumbnails the first time the report is opened, which makes that first visit slow.
