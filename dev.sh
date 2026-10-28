#!/usr/bin/env bash
# Start the app locally the safe way: debug on, local SQLite only, new migrations applied.
#   ./dev.sh                      run on http://127.0.0.1:8000
#   DEV_COUNTRY=FR ./dev.sh       pretend to be outside the US/UK (to see the withdrawal block)
set -euo pipefail
cd "$(dirname "$0")"

export DEBUG=True
unset DATABASE_URL SECURE_SSL_REDIRECT SECURE_HSTS_SECONDS   # never point local runs at a real database

[ -x .venv/bin/python ] || { echo "Missing .venv: run  uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt"; exit 1; }

.venv/bin/python manage.py migrate
echo
echo "Investor portal:  http://127.0.0.1:8000/investors/login/"
echo "Admin:            http://127.0.0.1:8000/admin/"
exec .venv/bin/python manage.py runserver "$@"
