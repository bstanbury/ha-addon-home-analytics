#!/bin/sh
set -e
CONFIG=/data/options.json
export HA_URL=$(python3 -c "import json; print(json.load(open('$CONFIG'))['ha_url'])")
export HA_TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG'))['ha_token'])")
export API_PORT=$(python3 -c "import json; print(json.load(open('$CONFIG'))['api_port'])")
echo "[INFO] Home Analytics Dashboard v1.0.0"
echo "[INFO] API port: ${API_PORT}"
exec python3 /app/server.py