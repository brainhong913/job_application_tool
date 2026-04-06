# Run Guide

This document explains how to run the **Job Search Dashboard** locally.

## 1) Open the repo

```bash
cd /home/hong/job_application
```

## 2) Activate the virtual environment

```bash
source .venv/bin/activate
```

If the virtual environment does not exist yet, create it with:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3) Configure the API key

Create or update `.secret` in the repo root:

```bash
cat > .secret <<'EOF'
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-4.1-mini
EOF
```

### Use API key from another repo
If your API key is already stored elsewhere, you can reuse it:

```bash
export JOB_SEARCH_SECRET_FILE=/path/to/other/repo/.secret
```

## 4) Start the dashboard

### Option A — direct Python run
```bash
python tools/dashboard.py --port 8000
```

### Option B — shell script
```bash
./run_dashboard.sh
```

If you want a different port:

```bash
./run_dashboard.sh 8080
```

## 5) Open in browser

Visit:

```text
http://127.0.0.1:8000
```

## Features available in the dashboard

- Track applications, contacts, and interviews
- Save an AI job-search prompt
- Add `Want` and `Don't want` filters
- Search online for official job recommendations
- Avoid duplicate recommendations
- Mark recommendation status as:
  - `Applied`
  - `Apply Later`
  - `Not Interested`

## Helpful files

- `tools/dashboard.py` — web dashboard
- `tools/job_tracker.py` — CLI tracker tool
- `data/recommendation_prompt.txt` — saved AI prompt
- `data/recommendation_filters.json` — saved want/don't-want filters
- `data/recommendations.csv` — saved recommendation results

## Stop the dashboard

In the terminal where it is running, press:

```bash
Ctrl+C
```
