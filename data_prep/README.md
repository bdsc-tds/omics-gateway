# Data preparation

These scripts turn our team's pipeline outputs into the layout the gateway reads (see "Data layout" in the main README). They run offline; the gateway never calls them.

| Script | Environment | Produces |
| --- | --- | --- |
| `generate_datasets_tsv.py` | `omics-gateway` | `datasets.tsv`, from the metadata of `.h5ad` files and `.zarr` stores |
| `convert_xenium.py` | `spatial` | a SpatialData `.zarr` store, from Xenium Ranger output |
| `write_store_metadata.py` | `spatial` | dataset metadata inside a `.zarr` store, from a YAML file |
| `generate_spatial_config.py` | `spatial` | the viewer configs for a store |
| `downscale_qc_figures.py` | `omics-gateway` | a lower-resolution copy of the QC figures |
| `build_qc_thumbnails.py` | `omics-gateway` | the QC report thumbnails |

The `spatial` environment is created with `conda env create -f data_prep/spatial_env.yaml`. Keep its packages out of `omics-gateway`, the environment the gateway runs in.

## Dataset table

`generate_datasets_tsv.py` searches the data directory recursively for `.h5ad` files and SpatialData `.zarr` stores, and fills one row per dataset from the `uns` metadata of its cell table. Our pipeline writes these keys into every `.h5ad` file; for a store they come from a YAML file (see below):

| Column | `uns` key |
| --- | --- |
| `dataset_id` | `dataset_name_short` |
| `name` | `dataset_name` (the file name if missing) |
| `description` | `article_title` |
| `assay`, `disease`, `tissue`, `sex`, `patients` | same names |
| `cell_count`, `gene_count` | same names, or the table's size when missing |
| `year` | the first four-digit year in `article_date` |
| `authors`, `journal`, `doi` | `article_authors`, `article_journal`, `article_doi` |

It removes `healthy` from `disease` and NA values from `tissue` and `sex`, and sorts multi-value fields with NA values last. Rows come in this order: `.h5ad` files, the merged meta-analysis file if given (with fields overridden from a YAML file), then the stores. A store without `dataset_name_short` in its metadata is skipped, so reference or scratch stores under `data/` stay out of the browser. Only the metadata keys and table sizes are read, not the tables themselves, which keeps memory use small even for the 349,000-cell meta-analysis.

```bash
conda run -n omics-gateway python data_prep/generate_datasets_tsv.py \
    --data-dir data \
    --output datasets.tsv \
    --merged-file data/meta_analysis_all_final_label_transfer_swapped.h5ad \
    --merged-config data_prep/meta_analysis_config.yaml
```

## Spatial datasets (Xenium)

The input is a Xenium Ranger output folder, plus an optional cell-type CSV with `cell_id` and `group` columns (ours is `Merged_celltypes_<sample>.csv`) and a metadata YAML file. The YAML maps the `uns` keys from the table above to strings or lists of strings; the counts can be left out:

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
``` The conversion uses these parts of the Xenium folder:

| Input | Becomes |
| --- | --- |
| `cells.parquet` | the cell table's metadata: centroids, areas and counts, used by the lasso, the crosshairs and the Metric layer |
| `cell_feature_matrix` | gene expression, used for gene colouring, the dot plot and the violin plot |
| `cell_boundaries.parquet` | cell outlines (Cell and Metric layers) |
| `nucleus_boundaries.parquet` | nucleus outlines (Nucleus layer) |
| `transcripts.parquet` | the Transcript layer's points |
| `morphology_focus/` | the image |
| `analysis/umap/` | the UMAP embedding |
| `experiment.xenium` | pixel size and run metadata |

1. Convert the folder. Always run it under a memory cap: writing the full-resolution image naively once exhausted a 16 GB machine.

    ```bash
    systemd-run --user --scope -p MemoryMax=5G -p MemorySwapMax=0 \
        conda run -n spatial python data_prep/convert_xenium.py \
            --xenium-dir /path/to/xenium_output \
            --out data/<sample>.zarr \
            --cell-types /path/to/Merged_celltypes_<sample>.csv \
            --metadata data/<sample>.metadata.yaml
    ```

    A 39,000-cell sample takes about two minutes and 3 GB on a 20-core machine. `--no-transcripts` and `--no-nucleus-boundaries` skip those elements. An unknown key in the metadata file stops the script before it converts anything. To add or correct metadata in an existing store without reconverting it:

    ```bash
    conda run -n spatial python data_prep/write_store_metadata.py \
        --zarr data/<sample>.zarr \
        --metadata data/<sample>.metadata.yaml
    ```

    Control-probe detections are dropped, and so are transcripts with a quality score (`qv`) below 20. Xenium Ranger's own cell counts, which drive gene colouring, use exactly the transcripts at or above 20, so the Transcript layer shows only molecules those counts include. Only the position and gene of each transcript are kept.

2. Generate the viewer configs, which land in `data/vitessce_configs/`:

    ```bash
    conda run -n spatial python data_prep/generate_spatial_config.py \
        --zarr data/<sample>.zarr
    ```

3. Regenerate `datasets.tsv` (see above) to add the store's row.

## QC reports

The expected folder layout is described in the main README. Our pipeline exports figures at 600 dpi, far more than a screen can show, so `downscale_qc_figures.py` writes a copy resampled to 150 dpi into a separate tree, leaving the originals untouched:

```bash
conda run -n omics-gateway python data_prep/downscale_qc_figures.py \
    --qc-data analysis_qc \
    --output analysis_qc_150dpi \
    --dpi 150
```

Check the result, swap the two trees by hand, then rebuild the thumbnails:

```bash
conda run -n omics-gateway python data_prep/build_qc_thumbnails.py \
    --qc-data analysis_qc
```

Run the thumbnail script after every QC sync too. Otherwise the gateway builds each report's thumbnails the first time it is opened, which makes that first visit slow.
