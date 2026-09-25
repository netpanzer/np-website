#!/bin/bash
# The orchestrator runs this file post-deployment. It is not for usage locally!
set -euo pipefail
cd "$(dirname "$0")"

python3.11 -m venv env
source env/bin/activate
pip3.11 install -r requirements.txt

python3.11 manage.py migrate
python3.11 manage.py collectstatic --skip-checks --no-input
python3.11 manage.py sync_web_release
exec env/bin/uvicorn netpanzer.asgi:application \
    --host 127.0.0.1 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 \
    --ws websockets-sansio --ws-max-size 1048576 \
    --limit-concurrency 128 --timeout-graceful-shutdown 10
