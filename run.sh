#!/bin/bash
# The orchestrator runs this file post-deployment. It is not for usage locally!

python3.11 -m venv env
source env/bin/activate
pip3.11 install -r requirements.txt

python3.11 manage.py migrate
python3.11 manage.py collectstatic --skip-checks --no-input
/home/winrid/np-website/env/bin/gunicorn \
          --access-logfile - \
          --error-logfile - \
          --workers 1 \
          netpanzer.wsgi:application
