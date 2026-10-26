#!/usr/bin/env bash
set -euo pipefail
RECRUITMENT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$RECRUITMENT_ROOT/.cache/uv}"
export npm_config_cache="${npm_config_cache:-$RECRUITMENT_ROOT/.cache/npm}"
cd "$RECRUITMENT_ROOT/backend"
uv run --frozen --extra dev ruff check app tests ../scripts/smoke.py ../scripts/patch_d365_full_name.py ../d365/validate_bundle.py
uv run --frozen python ../d365/validate_bundle.py
uv run --frozen --extra dev python -m pytest
cd "$RECRUITMENT_ROOT/frontend"
npm run lint
npm run typecheck
npm run test -- --run
npm run build
