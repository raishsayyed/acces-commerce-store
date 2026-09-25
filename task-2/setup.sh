#!/usr/bin/env bash
# One-time local setup for industrial-catalog-import
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit it with your credentials (see README.md)."
else
  echo ".env already exists — skipping copy."
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "Setup complete. Next steps:"
echo "  1. Edit .env with IMS + COMMERCE_API_BASE_URL (README.md § Credentials)"
echo "  2. source .venv/bin/activate"
echo "  3. python import_industrial_catalog.py --verify"
echo "  4. python import_industrial_catalog.py --all"
