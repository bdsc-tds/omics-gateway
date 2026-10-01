# Omics Gateway

Omics Gateway is a web portal for browsing and exploring a collection of omics datasets. It displays an index of the available datasets and opens each one in a suitable viewer. For `.h5ad` (AnnData) files, it launches a [cellxgene](https://github.com/chanzuckerberg/cellxgene) server that loads that particular file and, once it is available, proxies requests to it. Spatial datasets (`.zarr` SpatialData stores) open in a self-hosted [Vitessce](https://vitessce.io) viewer that runs entirely in the browser (see [Rebuilding the spatial viewer](#rebuilding-the-spatial-viewer)).

Omics Gateway started as a fork of [Novartis/cellxgene-gateway](https://github.com/Novartis/cellxgene-gateway), by Niket Patel, Yohann Potier and Alok Saldanha (Novartis Institutes for BioMedical Research), and has since been developed independently by CHUV. Both are released under the Apache License 2.0 (see `LICENSE`), and files from the original project keep their copyright headers.

## Installing

### Prerequisites

A conda installation, for example [Miniforge](https://github.com/conda-forge/miniforge). `deploy/setup.sh` creates the `omics-gateway` environment with everything else the gateway needs.

### Installing from a fresh clone

```bash
git clone https://github.com/bdsc-tds/omics-gateway.git
cd omics-gateway
./deploy/setup.sh
```

`deploy/setup.sh` creates the `omics-gateway` conda env from `deploy/omics-gateway_env.yaml` (using mamba if available, otherwise conda), installs this repo into it as an editable package, re-applies the cellxgene patch the gateway depends on, and creates the git-ignored `data/`, `analysis_qc/` and `logs/` directories. It is safe to re-run, needs no root, and honours `CONDA_ROOT`/`CONDA_ENV` just like `start_gunicorn.sh`.

Only editable installs are supported: the gateway serves its templates, static files and default branding straight from the repository, so a regular `pip install .` would give a package that cannot start.

Datasets are not tracked in git, so copy the files listed in `datasets.tsv` into `data/` afterwards.

## Configuring

The gateway is configured through environment variables:

* `CELLXGENE_LOCATION`: the location of the cellxgene executable, e.g. `~/anaconda2/envs/cellxgene/bin/cellxgene`

At least one of the following is required:
* `GATEWAY_DATA`: a directory that can contain subdirectories with `.h5ad` data files, *without* trailing slash, e.g. `/mnt/gateway_data`
* `GATEWAY_BUCKET`: an s3 bucket that can contain keys with `.h5ad` data files, e.g. `my-gateway-data-bucket`
Omics Gateway is designed to make it easy to add additional data sources, please see the source code for gateway.py and the ItemSource interface in items/item_source.py

Optional environment variables:
* `CELLXGENE_ARGS`: catch-all variable that can be used to pass additional command line args to cellxgene server
* `EXTERNAL_HOST`: the hostname and port from the perspective of the web browser, typically `localhost:5005` if running locally. Defaults to "localhost:{GATEWAY_PORT}"
* `EXTERNAL_PROTOCOL`: typically http when running locally, can be https when deployed if the gateway is behind a load balancer or reverse proxy that performs https termination. No default; when unset, the gateway does not override the scheme and Flask infers it from the incoming request
* `GATEWAY_IP`: ip addess of instance gateway is running on, mostly used to display SSH instructions. No default; when unset, `/metadata/ip_address` returns an empty response
* `GATEWAY_PORT`: local port that the gateway should bind to, defaults to 5005
* `GATEWAY_EXPIRE_SECONDS`: time in seconds that a cellxgene process will remain idle before being terminated. Defaults to 3600 (one hour)
* `GATEWAY_EXTRA_SCRIPTS`: JSON array of script paths, will be embedded into each page and forwarded with `--scripts` to cellxgene server
* `GATEWAY_ENABLE_ANNOTATIONS`: Set to `true` or to `1` to enable cellxgene annotations and gene sets.
* `GATEWAY_ENABLE_BACKED_MODE`: Set to `true` or to `1` to load AnnData in file-backed mode. This saves memory and speeds up launch time but may reduce overall performance.
* `GATEWAY_LOG_LEVEL`: default is `INFO`. set to `DEBUG` to increase logging and to `WARNING` to decrease logging.
* `DATASET_METADATA_TSV`: tab-separated file describing datasets, used to render the filterable dataset browser at `/filecrawl`. Defaults to `datasets.tsv`. When the file is absent, the browser falls back to listing files from the configured item sources
* `QC_DATA`: a directory containing per-dataset QC report folders, served at `/qc/<dataset_id>`. Defaults to `analysis_qc`. A relative path is resolved against the working directory
* `QC_THUMB_CACHE`: a directory for the thumbnails shown in QC reports, which the gateway builds on demand. Defaults to `<QC_DATA>_thumbs`, outside the QC tree so that tree can stay read-only
* `SPATIAL_METRICS`: Set to `false` or to `0` to open spatial (`.zarr`) datasets without the Metric layer, which colours cells by per-cell measurements such as cell area. Defaults to `true`. `data_prep/generate_spatial_config.py` writes both configs for each store (`<name>.vitessce.json` and `<name>.nometrics.vitessce.json`), and this variable picks which one the dataset browser links to
* `GATEWAY_BRANDING`: path to the branding file of the deployment (names, logo, favicon and homepage texts). Defaults to `branding/default/branding.yaml`, which shows neutral Omics Gateway branding. See [Branding](#branding)
* `S3_ENABLE_LISTINGS_CACHE`: Set to `true` or to `1` to cache listings of S3 folders for performance. If the cache becomes stale, set `filecrawl?refresh=true` query parameter to refresh the cache.

If any of the following optional variables are set, [ProxyFix](https://werkzeug.palletsprojects.com/en/1.0.x/middleware/proxy_fix/) will be used.
* `PROXY_FIX_FOR`: Number of upstream proxies setting X-Forwarded-For
* `PROXY_FIX_PROTO`: Number of upstream proxies setting X-Forwarded-Proto
* `PROXY_FIX_HOST`: Number of upstream proxies setting X-Forwarded-Host
* `PROXY_FIX_PORT`: Number of upstream proxies setting X-Forwarded-Port
* `PROXY_FIX_PREFIX`: Number of upstream proxies setting X-Forwarded-Prefix

### Branding

The page titles, navbar logo and title, favicon, and homepage texts and contact banner come from a branding file rather than from the templates. To brand a deployment, copy `branding/default/` to `branding/<name>/` (git ignores every branding folder except the default one), edit its `branding.yaml`, whose comments describe each setting, and set `GATEWAY_BRANDING` to that file. The gateway checks the file when it starts and refuses to start if a mandatory setting is missing, a setting is unknown or an image cannot be found, naming each problem. Changes to the file take effect when the gateway restarts.

The homepage's dataset and cell counts are computed from the dataset table on every visit, so they follow edits to it without a restart. The number of cell types is set in the branding file.

## Data layout

The gateway reads datasets from three places, none of them tracked in git: the data directory (`GATEWAY_DATA`, `data/` with `start_gunicorn.sh`), the dataset table (`DATASET_METADATA_TSV`, `datasets.tsv`) and the QC directory (`QC_DATA`, `analysis_qc/`).

```
data/
    <dataset>.h5ad                          single-cell dataset, opened in cellxgene
    <dataset>_annotations/                  optional, next to its .h5ad
        <name>.csv                          cell annotations, loadable in cellxgene
        <name>_gene_sets.csv                gene sets, offered for download only
    <sample>.zarr/                          SpatialData store, opened in the spatial viewer
    vitessce_configs/
        <sample>.vitessce.json              viewer config, with the Metric layer
        <sample>.nometrics.vitessce.json    viewer config, without it
        <sample>.metrics.json               per-cell values shown in cell tooltips
analysis_qc/
    <dataset_id>/                           one folder per dataset_id in datasets.tsv
        <N>_<step>/                         e.g. 1_preprocessing, 2_normalisation
            [<section>/]                    optional, e.g. 1_raw, results (see below)
                <figure>.jpg                shown for all samples together
                per_sample/<sample>_QC_<figure>.jpg
```

### Dataset table

`datasets.tsv` is tab-separated, with one row per dataset and these columns:

* `dataset_id`: short identifier, also the name of the dataset's QC folder
* `name`, `description`: shown in the dataset browser
* `file_path`: path of the `.h5ad` file or `.zarr` store, relative to the data directory. A `.zarr` row is shown as spatial and opens its viewer config
* `assay`, `disease`, `tissue`, `sex`: semicolon-separated values, used as filters
* `patients`, `cell_count`, `gene_count`, `year`: numbers, used for display and as range filters
* `authors`, `journal`, `doi`: publication details

### Spatial datasets

A spatial dataset is a SpatialData `.zarr` store plus the viewer configs generated for it. The configs must live in `data/vitessce_configs/` and be named after the store, because the viewer loads them, like the store itself, through `/spatial-data/`, the only route that serves byte ranges. `SPATIAL_METRICS` picks which of the two configs the dataset browser links to.

### QC reports

`/qc/<dataset_id>` shows one tab per step folder, in folder-name order. Inside a step, folders named `1_raw`, `2_filtered`, `filtered`, `3_doublets`, `results` or `training` become headed sections; a step without them is shown as one section. Figures (`.jpg`, `.jpeg`, `.png` or `.svg`) under `per_sample/`, `per_dataset/` or `3_doublets/` are grouped by the part of their file name before `_QC_` or `_doublet_`; all other figures are shown together. The gateway builds thumbnails into `QC_THUMB_CACHE` the first time a report is opened; `data_prep/build_qc_thumbnails.py` builds them all in advance, so run it after each QC sync:

```bash
conda run -n omics-gateway python data_prep/build_qc_thumbnails.py --qc-data analysis_qc
```

## Running the gateway

### With gunicorn

Use the gunicorn start script:

```bash
( ./start_gunicorn.sh )
```

Configuration is inlined at the top of `start_gunicorn.sh`. Paths derive from the conda env (`CONDA_ENV`, default `omics-gateway`) and the repo location, so the script is host-independent. Every setting is written as `${VAR:-default}`, so any of them can still be overridden from the environment:

```bash
GATEWAY_DATA=/path/to/data ( ./start_gunicorn.sh )
```

In production the script runs under a systemd service, which can override settings with `Environment=` directives.

nginx and TLS are host-specific and are not covered here. For systemd, adapt `deploy/omics-gateway.service.example` by editing the paths and `User=` for the host.

### Ad hoc, for development

1. Prepare a folder with .h5ad files, for example

```bash
mkdir ../gateway_data
wget https://raw.githubusercontent.com/chanzuckerberg/cellxgene/master/example-dataset/pbmc3k.h5ad -O ../gateway_data/pbmc3k.h5ad
```

2. In the activated environment (`conda activate omics-gateway`), set the required environment variables (see [Configuring](#configuring)):

```bash
export GATEWAY_DATA=../gateway_data  # Change this if you put data in a different place
export CELLXGENE_LOCATION=`which cellxgene`
```

3. Now, execute the gateway:

```bash
omics-gateway
```

## Updating

Pull the changes:

```bash
git stash  # Stash local changes if needed
git pull
git stash pop  # Reapply stashed changes if needed
```

Then restart the gateway, for example with `sudo systemctl restart omics-gateway` under systemd. The repository is installed in editable mode, so the restart picks up code changes. `deploy/setup.sh` leaves an existing environment alone, so if `deploy/omics-gateway_env.yaml` changed, remove the environment with `conda env remove -n omics-gateway` and re-run `./deploy/setup.sh`.

## Development

GitHub Actions (`.github/workflows/pr-checks.yaml`) runs the tests and linting below on every pull request and every push to `main`; the tests run in an environment built from `deploy/omics-gateway_env.yaml`. It can also be started by hand on any branch from the repository's Actions tab, once the workflow is on `main`.

### Running tests

```bash
conda run -n omics-gateway python -m unittest discover tests
```

### Linting

Linting uses [ruff](https://docs.astral.sh/ruff/) 0.16.0, configured in `ruff.toml`. Ruff is not part of the `omics-gateway` environment, so run it from any environment that has it:

```bash
ruff check .
ruff format --check .
```

Front-end code is checked with [Biome](https://biomejs.dev/) 2.5.14, configured in `biome.jsonc`. It lints and formats the JavaScript and CSS (the gateway's own, and the spatial viewer's source in `spatial_viewer_src/`), and lints the templates, which stay formatted by hand. Create its environment once, then run it from the repository root:

```bash
conda env create -f biome_env.yaml
conda run -n biome biome ci               # lint and check formatting, as CI does
conda run -n biome biome format --write   # apply formatting
```

### Rebuilding the spatial viewer

Spatial datasets open in [Vitessce](https://vitessce.io), a JavaScript application that runs entirely in the visitor's browser: the gateway only serves files. Browsers cannot load npm packages directly, so [Vite](https://vite.dev) builds Vitessce and its dependencies into plain JavaScript files in `omics_gateway/static/vitessce/`, which `omics_gateway/templates/spatial_viewer.html` loads. The build inputs live in `spatial_viewer_src/`:

* `main.js`: the entry point, which reads the page's `?config=` parameter, fetches that Vitessce config and mounts the viewer
* `vite.config.js`: build settings (output directory, browser shims for Node globals)
* `package.json` and `package-lock.json`: the npm packages, pinned to exact versions
* `viewer_build_env.yaml`: the conda environment providing Node

The built files are committed, so deployment needs neither Node nor a build step, and no CDN is contacted at runtime. Rebuild only after upgrading Vitessce or editing `main.js` or `vite.config.js`:

```bash
conda env create -f spatial_viewer_src/viewer_build_env.yaml   # once
cd spatial_viewer_src
conda run -n viewer-build npm ci          # installs the locked packages into node_modules/ (about 1.8 GB)
conda run -n viewer-build npm run build   # replaces the contents of ../omics_gateway/static/vitessce/
rm -rf node_modules                       # optional, frees the disk space
```

The build is reproducible: rebuilding unchanged sources gives identical files. To upgrade Vitessce, run `conda run -n viewer-build npm install --save-exact vitessce@<version>` in `spatial_viewer_src/`, which updates `package.json` and `package-lock.json`, then build as above, update the version in `CREDITS.md`, and commit the sources and the built files together.

`spatial-viewer.js` keeps a fixed name, so it must be served with revalidation rather than long-term caching; the other built files have content hashes in their names and can be cached indefinitely.

The spatial viewer page also adjusts Vitessce at runtime, through `omics_gateway/static/css/spatial_viewer.css` and `omics_gateway/static/js/spatial_viewer.js` (legend fixes, layer order, lasso behaviour). Some of these rely on Vitessce internals, so check the viewer in a browser after an upgrade, including a spatial lasso with only the Nucleus layer visible. The lasso fix logs `Lasso override not applied` to the browser console when it cannot find what it patches, but not every breakage is detectable.

## Getting help

If you run into a problem or have a question, please open an issue on [GitHub](https://github.com/bdsc-tds/omics-gateway/issues), describing what you did, what you expected and what happened instead.

## Contributing and Code of Conduct

Interested in contributing? Pull requests are welcome! Check out the [contributing guidelines](CONTRIBUTING.md). Please note that this project is released with a [Contributor Code of Conduct](CONDUCT.md). By contributing to this project, you agree to abide by its terms.
