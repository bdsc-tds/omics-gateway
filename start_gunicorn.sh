#!/bin/bash

# start_gunicorn.sh - Start Omics Gateway with Gunicorn
#
# PREREQUISITES:
# - Gunicorn installed (included with cellxgene 1.3.0, or: pip install gunicorn)
# - Conda env named by CONDA_ENV (default: omics-gateway)
#
# USAGE:
# ./start_gunicorn.sh
#
# Configuration lives here, not in .env: every setting below is written as
# ${VAR:-default}, so environment still overrides it (e.g. systemd
# Environment= or inline export)

# Exit on error
set -e

# Get directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"


# Server config; paths derive from conda env and repo, so host-independent
# Conda variables
CONDA_ROOT=${CONDA_ROOT:-$HOME/miniforge3}
CONDA_ENV=${CONDA_ENV:-omics-gateway}
CONDA_ENV_BIN="$CONDA_ROOT/envs/$CONDA_ENV/bin"

# Cellxgene variables
export PATH="$CONDA_ENV_BIN:$PATH"
export CELLXGENE_LOCATION=${CELLXGENE_LOCATION:-$CONDA_ENV_BIN/cellxgene}
export GATEWAY_DATA=${GATEWAY_DATA:-$SCRIPT_DIR/data}
export QC_DATA=${QC_DATA:-$SCRIPT_DIR/analysis_qc}
export GATEWAY_LOG_LEVEL=${GATEWAY_LOG_LEVEL:-INFO}
export GATEWAY_IP=${GATEWAY_IP:-127.0.0.1}

# Trust forwarded headers set by reverse proxy
export PROXY_FIX_FOR=${PROXY_FIX_FOR:-1}
export PROXY_FIX_PROTO=${PROXY_FIX_PROTO:-1}
export PROXY_FIX_HOST=${PROXY_FIX_HOST:-1}
export PROXY_FIX_PREFIX=${PROXY_FIX_PREFIX:-1}

# Check cellxgene binary exists: defaults always set, but may point nowhere
if [ ! -x "$CELLXGENE_LOCATION" ]; then
    echo "Error: cellxgene not found at $CELLXGENE_LOCATION"
    echo "Set CELLXGENE_LOCATION, or CONDA_ROOT/CONDA_ENV (currently:"
    echo "  CONDA_ROOT=$CONDA_ROOT, CONDA_ENV=$CONDA_ENV)"
    exit 1
fi

# Check data directory exists: gateway only checks it is set, on first request
if [ ! -d "$GATEWAY_DATA" ]; then
    echo "Error: data directory not found at $GATEWAY_DATA"
    echo "Set GATEWAY_DATA, or create $SCRIPT_DIR/data (deploy/setup.sh does)"
    exit 1
fi

# Gunicorn config: one worker, since each keeps own in-memory cache and peers
# 404 on its datasets (https://github.com/Novartis/cellxgene-gateway/pull/99)
WORKERS=${GUNICORN_WORKERS:-1}
# gthread workers serve requests on threads sharing one BackendCache, so
# multi-worker cache sync issues never arise
WORKER_CLASS=${GUNICORN_WORKER_CLASS:-gthread}
THREADS=${GUNICORN_THREADS:-8}
BIND=${GATEWAY_IP:-0.0.0.0}:${GATEWAY_PORT:-5005}
TIMEOUT=${GUNICORN_TIMEOUT:-120}
KEEPALIVE=${GUNICORN_KEEPALIVE:-5}
LOG_LEVEL=${GUNICORN_LOG_LEVEL:-info}

# Production optimisation: enable backed mode to reduce memory usage
export GATEWAY_ENABLE_BACKED_MODE=${GATEWAY_ENABLE_BACKED_MODE:-true}

# Spatial viewer: false links no-Metric configs (generator writes both)
export SPATIAL_METRICS=${SPATIAL_METRICS:-true}

# Check if gunicorn is installed
if ! command -v gunicorn &> /dev/null; then
    echo "Error: gunicorn not found. Install with: pip install gunicorn"
    exit 1
fi

# Display configuration
echo "Starting Omics Gateway with Gunicorn..."
echo "Configuration:"
echo "  Cellxgene executable: ${CELLXGENE_LOCATION}"
echo "  Data source: ${GATEWAY_DATA:-$GATEWAY_BUCKET}"
echo "  QC data: ${QC_DATA}"
echo "  Branding: ${GATEWAY_BRANDING:-default}"
echo "  Binding to: $BIND"
echo "  Workers: $WORKERS"
echo "  Worker class: $WORKER_CLASS"
echo "  Threads per worker: $THREADS"
echo "  Timeout: ${TIMEOUT}s"
echo "  Keepalive: ${KEEPALIVE}s"
echo "  Log level: $LOG_LEVEL"
echo "  Gateway log level: ${GATEWAY_LOG_LEVEL}"
echo "  Backed mode: ${GATEWAY_ENABLE_BACKED_MODE}"
echo "  Spatial metrics: ${SPATIAL_METRICS}"
echo "  Proxy fix (for/proto/host/prefix): ${PROXY_FIX_FOR}/${PROXY_FIX_PROTO}/${PROXY_FIX_HOST}/${PROXY_FIX_PREFIX}"
echo ""

cd "$SCRIPT_DIR"

# Optional: GUNICORN_MAX_REQUESTS restarts worker after N requests (memory
# leaks), GUNICORN_MAX_REQUESTS_JITTER randomises that count
exec gunicorn omics_gateway.gateway:app \
    --workers "$WORKERS" \
    --worker-class "$WORKER_CLASS" \
    --threads "$THREADS" \
    --bind "$BIND" \
    --timeout "$TIMEOUT" \
    --keep-alive "$KEEPALIVE" \
    --error-logfile - \
    --log-level "$LOG_LEVEL" \
    --preload \
    ${GUNICORN_MAX_REQUESTS:+--max-requests "$GUNICORN_MAX_REQUESTS"} \
    ${GUNICORN_MAX_REQUESTS_JITTER:+--max-requests-jitter "$GUNICORN_MAX_REQUESTS_JITTER"} \
    "$@"
