# Job Search Tracker Repo

A simple personal repo to **track applications, record outreach/interviews, and keep practical recommendations** for your job search.

## Structure

- `data/` — CSV trackers for applications, contacts, and interviews
- `templates/` — reusable notes and outreach templates
- `recommendations/` — job search playbook and review habits
- `tools/` — lightweight Python CLI plus a local web dashboard

## Quick Start

### 1) Track activity
Update the CSV files directly or use the helper tool.

```bash
python3 tools/job_tracker.py summary
python3 tools/job_tracker.py recommend
python3 tools/job_tracker.py add-application \
  --company "Example Inc" \
  --role "Backend Engineer" \
  --location "Remote" \
  --status "Applied" \
  --url "https://example.com/jobs/123"
```

### 2) Configure AI job recommendations

Create a local `.secret` file in the repo root and paste your API key there.
If you already keep it in another repo, either copy that file here or start the dashboard with `JOB_SEARCH_SECRET_FILE=/path/to/other/repo/.secret`.

```bash
cat > .secret <<'EOF'
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-4.1-mini
EOF
```

### 3) Launch the web dashboard

```bash
python3 tools/dashboard.py --port 8000
```

Or use the helper script:

```bash
./run_dashboard.sh
```

Then open `http://127.0.0.1:8000` in your browser.

For a full run guide, see `RUN.md`.

Inside the dashboard you can:
- save a reusable prompt for AI job search
- search online for fresh official job postings
- avoid duplicate recommendations from your tracked list
- mark each recommendation as `Applied`, `Apply Later`, or `Not Interested`

### 4) Suggested workflow

- Add every application to `data/applications.csv`
- Record networking in `data/contacts.csv`
- Log interviews in `data/interviews.csv`
- Review `recommendations/job-search-playbook.md` once a week
- Use `templates/` when preparing outreach or application notes

## Status Values

Recommended application statuses:

- `Wishlist`
- `Applied`
- `Follow-Up`
- `Interview`
- `Offer`
- `Rejected`
- `Closed`

## Goal

Use this repo as your single source of truth for:

- what you applied to
- who you contacted
- which interviews are coming up
- where to improve next week
