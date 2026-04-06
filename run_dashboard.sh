#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8000}"

cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

if [[ -n "${JOB_SEARCH_SECRET_FILE:-}" ]]; then
  echo "Using secret file from: $JOB_SEARCH_SECRET_FILE"
elif [[ -f ".secret" ]]; then
  echo "Using local .secret file"
else
  echo "Warning: no .secret file found and JOB_SEARCH_SECRET_FILE is not set."
  echo "AI recommendation search will not work until OPENAI_API_KEY is configured."
fi

echo "Starting Job Search Dashboard on http://127.0.0.1:${PORT}"
python tools/dashboard.py --port "$PORT"
