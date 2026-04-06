from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date, timedelta
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from job_tracker import APPLICATIONS_FILE, CONTACTS_FILE, INTERVIEWS_FILE, ROOT, parse_date, read_rows

DATA_DIR = ROOT / "data"
RECOMMENDATIONS_FILE = DATA_DIR / "recommendations.csv"
PROMPT_FILE = DATA_DIR / "recommendation_prompt.txt"
FILTERS_FILE = DATA_DIR / "recommendation_filters.json"
CV_TEXT_FILE = DATA_DIR / "cv_text.txt"
SECRET_FILE = ROOT / ".secret"
CV_UPLOAD_MAX_BYTES = 10 * 1024 * 1024

RECOMMENDATION_HEADERS = [
    "id",
    "date_added",
    "company",
    "role",
    "location",
    "official_url",
    "reason",
    "status",
    "source",
    "notes",
]

DEFAULT_PROMPT = """Find 5 current job openings that fit my background and interests. Prioritize official company careers pages or official ATS links, remote or US-based roles, and explain briefly why each role matches."""
ALLOWED_RECOMMENDATION_STATUSES = {"New", "Applied", "Apply Later", "Not Interested"}
TAB_ORDER = ["feed", "applied", "later", "not-interested", "all"]
TAB_LABELS = {
    "feed": "Feed",
    "applied": "Applied",
    "later": "Apply Later",
    "not-interested": "Not Interested",
    "all": "All",
}
TAB_STATUSES = {
    "feed": {"New"},
    "applied": {"Applied"},
    "later": {"Apply Later"},
    "not-interested": {"Not Interested"},
    "all": None,
}
UNOFFICIAL_HOST_FRAGMENTS = {
    "indeed.com",
    "linkedin.com",
    "wellfound.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "careerjet.com",
    "simplyhired.com",
    "remoterocketship.com",
    "remotefirstjobs.com",
    "nodesk.co",
    "remote.co",
    "totaljobs.com",
    "artificialintelligencejobs.co.uk",
    "huntukvisasponsors.com",
    "visasponsor.jobs",
    "reed.co.uk",
    "cv-library.co.uk",
    "adzuna.",
    "jooble.",
}
KNOWN_ATS_HOST_FRAGMENTS = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "smartrecruiters.com",
    "jobvite.com",
    "icims.com",
    "workable.com",
    "bamboohr.com",
}
THIRD_PARTY_HOST_HINTS = {
    "recruit",
    "recruiting",
    "recruiter",
    "staffing",
    "agency",
    "headhunt",
    "talent",
    "sponsor",
    "visa",
}
COMMON_COMPANY_WORDS = {
    "the",
    "and",
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "company",
    "co",
    "group",
    "technology",
    "technologies",
}


def ensure_support_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not RECOMMENDATIONS_FILE.exists():
        with RECOMMENDATIONS_FILE.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=RECOMMENDATION_HEADERS)
            writer.writeheader()
    if not PROMPT_FILE.exists():
        PROMPT_FILE.write_text(DEFAULT_PROMPT + "\n", encoding="utf-8")
    if not FILTERS_FILE.exists():
        FILTERS_FILE.write_text(json.dumps({"want": "", "dont_want": "", "location": ""}, indent=2) + "\n", encoding="utf-8")
    if not CV_TEXT_FILE.exists():
        CV_TEXT_FILE.write_text("", encoding="utf-8")


def write_rows(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def read_recommendation_rows() -> list[dict[str, str]]:
    ensure_support_files()
    rows = read_rows(RECOMMENDATIONS_FILE)
    filtered_rows = [
        row for row in rows if is_official_job_url(row.get("official_url", ""), row.get("company", ""))
    ]
    if len(filtered_rows) != len(rows):
        write_rows(RECOMMENDATIONS_FILE, RECOMMENDATION_HEADERS, filtered_rows)
    return filtered_rows


def load_saved_prompt() -> str:
    ensure_support_files()
    prompt = PROMPT_FILE.read_text(encoding="utf-8").strip()
    return prompt or DEFAULT_PROMPT


def save_prompt(prompt: str) -> None:
    ensure_support_files()
    cleaned = prompt.strip() or DEFAULT_PROMPT
    PROMPT_FILE.write_text(cleaned + "\n", encoding="utf-8")


def load_saved_cv_text() -> str:
    ensure_support_files()
    return CV_TEXT_FILE.read_text(encoding="utf-8").strip()


def save_cv_text(cv_text: str) -> None:
    ensure_support_files()
    CV_TEXT_FILE.write_text(cv_text.strip() + "\n" if cv_text.strip() else "", encoding="utf-8")


def extract_text_from_pdf_bytes(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF upload support needs `pypdf` installed in the virtual environment.") from exc

    reader = PdfReader(io.BytesIO(file_bytes))
    pages: list[str] = []
    for page in reader.pages:
        page_text = (page.extract_text() or "").strip()
        if page_text:
            pages.append(page_text)
    return "\n\n".join(pages).strip()


def extract_text_from_docx_bytes(file_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
        try:
            xml_bytes = archive.read("word/document.xml")
        except KeyError as exc:
            raise ValueError("The uploaded DOCX file is missing `word/document.xml`.") from exc

    root = ET.fromstring(xml_bytes)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def extract_text_from_uploaded_cv(filename: str, file_bytes: bytes) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not filename or not file_bytes:
        raise ValueError("Choose a PDF or DOCX file to upload.")
    if len(file_bytes) > CV_UPLOAD_MAX_BYTES:
        raise ValueError("The uploaded CV is too large. Keep it under 10 MB.")

    if suffix == ".pdf":
        text = extract_text_from_pdf_bytes(file_bytes)
    elif suffix == ".docx":
        text = extract_text_from_docx_bytes(file_bytes)
    elif suffix in {".txt", ".md"}:
        text = file_bytes.decode("utf-8", errors="ignore").strip()
    else:
        raise ValueError("Unsupported file type. Upload a PDF or DOCX resume.")

    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not cleaned:
        raise ValueError(
            "I couldn't extract readable text from that file. Try another PDF/DOCX or paste the text manually."
        )
    return cleaned


def normalize_tag_values(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []

    raw_items = value if isinstance(value, list) else re.split(r"[,\n]+", str(value))
    cleaned_items: list[str] = []
    seen: set[str] = set()

    for item in raw_items:
        cleaned = re.sub(r"\s+", " ", str(item)).strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned_items.append(cleaned)

    return cleaned_items


def normalize_site_host(url: str) -> str:
    parsed = urllib.parse.urlparse((url or "").strip())
    host = (parsed.netloc or parsed.path).strip().lower()
    return host[4:] if host.startswith("www.") else host


def load_saved_filters() -> dict[str, str | list[str]]:
    ensure_support_files()
    try:
        payload = json.loads(FILTERS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return {
            "want": "",
            "dont_want": "",
            "location": "",
            "result_count": "5",
            "want_tags": [],
            "dont_want_tags": [],
            "ignored_sites": [],
        }

    want_tags = normalize_tag_values(payload.get("want_tags", payload.get("want", "")))
    dont_want_tags = normalize_tag_values(payload.get("dont_want_tags", payload.get("dont_want", "")))
    ignored_sites = normalize_tag_values(payload.get("ignored_sites", []))
    result_count = str(payload.get("result_count", "5")).strip() or "5"
    return {
        "want": ", ".join(want_tags),
        "dont_want": ", ".join(dont_want_tags),
        "location": str(payload.get("location", "")).strip(),
        "result_count": result_count,
        "want_tags": want_tags,
        "dont_want_tags": dont_want_tags,
        "ignored_sites": ignored_sites,
    }


def save_filters(
    want: str | list[str],
    dont_want: str | list[str],
    location: str = "",
    ignored_sites: str | list[str] | None = None,
    result_count: str | int | None = None,
) -> None:
    ensure_support_files()
    want_tags = normalize_tag_values(want)
    dont_want_tags = normalize_tag_values(dont_want)
    existing_filters = load_saved_filters()
    saved_ignored_sites = existing_filters.get("ignored_sites", []) if ignored_sites is None else ignored_sites
    clean_ignored_sites = normalize_tag_values(saved_ignored_sites)
    clean_result_count = str(result_count if result_count is not None else existing_filters.get("result_count", "5")).strip() or "5"
    try:
        count_number = max(1, min(20, int(clean_result_count)))
    except ValueError:
        count_number = 5
    payload = {
        "want": ", ".join(want_tags),
        "dont_want": ", ".join(dont_want_tags),
        "location": location.strip(),
        "result_count": str(count_number),
        "want_tags": want_tags,
        "dont_want_tags": dont_want_tags,
        "ignored_sites": clean_ignored_sites,
    }
    FILTERS_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def compose_search_preferences(
    prompt: str,
    want: str,
    dont_want: str,
    location: str = "",
    cv_text: str = "",
) -> str:
    sections = [f"Saved prompt:\n{prompt.strip() or DEFAULT_PROMPT}"]
    if cv_text.strip():
        sections.append(f"Candidate CV / resume:\n{cv_text.strip()[:6000]}")
    if want.strip():
        sections.append(f"Want:\n{want.strip()}")
    if dont_want.strip():
        sections.append(f"Don't want:\n{dont_want.strip()}")
    if location.strip():
        sections.append(f"Preferred location:\n{location.strip()}")
    return "\n\n".join(sections)


def load_secret_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_api_config() -> tuple[str, str, str]:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    env_model = os.environ.get("OPENAI_MODEL", "").strip()
    if env_key:
        return env_key, env_model or "gpt-4.1-mini", "environment"

    secret_path_text = os.environ.get("JOB_SEARCH_SECRET_FILE", "").strip()
    secret_path = Path(secret_path_text).expanduser() if secret_path_text else SECRET_FILE
    if secret_path.exists():
        values = load_secret_values(secret_path)
        return values.get("OPENAI_API_KEY", "").strip(), values.get("OPENAI_MODEL", "gpt-4.1-mini").strip(), str(secret_path)

    return "", env_model or "gpt-4.1-mini", str(secret_path)


def get_api_status_message() -> str:
    api_key, model, source = get_api_config()
    if api_key:
        return f"OpenAI is configured via {source} using model {model}."
    return f"OpenAI API key not found. Add `OPENAI_API_KEY=...` to `.secret` or set `JOB_SEARCH_SECRET_FILE` to your other repo's `.secret` file. Checked: {source}"


def build_tracker_insights(
    apps: list[dict[str, str]],
    contacts: list[dict[str, str]],
    interviews: list[dict[str, str]],
) -> list[str]:
    status_counts = Counter((row.get("status") or "Unknown").strip() or "Unknown" for row in apps)
    insights: list[str] = []

    if len(apps) < 10:
        insights.append("Build a target list of 20–30 companies and prioritize strong-fit roles.")

    if len(apps) > 0 and status_counts.get("Interview", 0) == 0 and len(interviews) == 0:
        insights.append("Tailor your resume more tightly to each role if applications are not converting.")

    if status_counts.get("Rejected", 0) >= 5:
        insights.append("Review rejected roles for patterns in skill gaps, level mismatch, or missing keywords.")

    if len(contacts) < max(3, len(apps) // 3):
        insights.append("Increase networking: aim for 3–5 targeted outreach messages each week.")

    has_pending = any((row.get("status") or "").strip() in {"Applied", "Follow-Up", "Interview"} for row in apps)
    has_followups = any(parse_date(row.get("follow_up_date")) for row in apps)
    if has_pending and not has_followups:
        insights.append("Add follow-up dates for pending applications so opportunities do not go stale.")

    if len(interviews) > 0:
        insights.append("Keep a concise bank of STAR stories and company-specific questions ready.")

    if not insights:
        insights.append("Your tracker looks healthy. Stay consistent and review progress weekly.")

    return insights


def render_table(columns: list[tuple[str, str]], rows: list[dict[str, str]], empty_message: str) -> str:
    if not rows:
        return f'<div class="empty">{html.escape(empty_message)}</div>'

    headers = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body_rows: list[str] = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key, "") or "—"
            if key.endswith("url") and value != "—":
                safe_url = html.escape(value, quote=True)
                cells.append(f'<td><a href="{safe_url}" target="_blank" rel="noreferrer">Open link</a></td>')
            else:
                cells.append(f"<td>{html.escape(str(value))}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def normalize_job_key(company: str, role: str, url: str) -> str:
    company_part = re.sub(r"\W+", " ", (company or "").lower()).strip()
    role_part = re.sub(r"\W+", " ", (role or "").lower()).strip()
    parsed = urllib.parse.urlsplit((url or "").strip())
    url_part = f"{parsed.netloc.lower()}{parsed.path}".rstrip("/")
    return f"{company_part}|{role_part}|{url_part}"


def company_name_tokens(company: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (company or "").lower())
    return [token for token in tokens if len(token) > 2 and token not in COMMON_COMPANY_WORDS]


def collect_existing_jobs(apps: list[dict[str, str]], recommendations: list[dict[str, str]]) -> tuple[set[str], list[str]]:
    keys: set[str] = set()
    descriptions: list[str] = []

    for row in apps:
        company = row.get("company", "")
        role = row.get("role", "")
        url = row.get("job_url", "")
        key = normalize_job_key(company, role, url)
        if key.strip("|"):
            keys.add(key)
            descriptions.append(f"{company} | {role} | {url or 'no-url'}")

    for row in recommendations:
        company = row.get("company", "")
        role = row.get("role", "")
        url = row.get("official_url", "")
        key = normalize_job_key(company, role, url)
        if key.strip("|"):
            keys.add(key)
            descriptions.append(f"{company} | {role} | {url or 'no-url'}")

    return keys, descriptions


def extract_output_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])

    chunks: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def extract_json_blob(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped

    object_match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if object_match:
        return object_match.group(0)

    list_match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if list_match:
        return list_match.group(0)

    return stripped


def parse_jobs_from_response(text: str) -> list[dict[str, str]]:
    blob = extract_json_blob(text)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"The model response was not valid JSON: {exc}") from exc

    items = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError("The model response did not include a `jobs` list.")

    parsed_jobs: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        company = str(item.get("company", "")).strip()
        role = str(item.get("role", "")).strip()
        location = str(item.get("location", "")).strip()
        official_url = str(item.get("official_url") or item.get("url") or item.get("job_url") or "").strip()
        reason = str(item.get("reason", "")).strip()
        source = urllib.parse.urlparse(official_url).netloc or str(item.get("source", "OpenAI web search")).strip() or "OpenAI web search"

        if company and role and official_url:
            parsed_jobs.append(
                {
                    "company": company,
                    "role": role,
                    "location": location,
                    "official_url": official_url,
                    "reason": reason,
                    "source": source,
                }
            )

    if not parsed_jobs:
        raise RuntimeError("No valid jobs were returned. Try a more specific prompt.")

    return parsed_jobs


def is_official_job_url(url: str, company: str = "") -> bool:
    parsed = urllib.parse.urlparse((url or "").strip())
    host = normalize_site_host(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    combined = f"{host}{path}?{query}"
    if not host:
        return False

    if any(fragment in combined for fragment in UNOFFICIAL_HOST_FRAGMENTS):
        return False
    if any(hint in host for hint in THIRD_PARTY_HOST_HINTS):
        return False

    company_tokens = company_name_tokens(company)
    has_company_match = any(token in combined for token in company_tokens)

    if any(host.endswith(fragment) for fragment in KNOWN_ATS_HOST_FRAGMENTS):
        if host.startswith("job-boards.") or (host.startswith("boards.") and path.startswith("/embed/")):
            return False
        return has_company_match if company_tokens else True

    if any(word in host for word in {"jobs", "job", "boards", "board"}) and not has_company_match:
        return False

    if company_tokens and any(word in host for word in {"careers", "career", "talent", "hiring"}):
        return has_company_match

    return True


def fetch_ai_job_recommendations(
    prompt: str,
    want: str,
    dont_want: str,
    location: str,
    cv_text: str,
    result_count: int,
    seen_keys: set[str],
    seen_descriptions: list[str],
) -> list[dict[str, str]]:
    api_key, model, _ = get_api_config()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY. Add it to `.secret` or point `JOB_SEARCH_SECRET_FILE` to the other repo's `.secret` file.")

    existing_text = "\n".join(f"- {item}" for item in seen_descriptions[:80]) or "- none yet"
    instructions = (
        "Search the public web for current job openings and return ONLY JSON in this exact shape: "
        '{"jobs":[{"company":"","role":"","location":"","official_url":"","reason":"","source":""}]}\n'
        "Rules:\n"
        "- The official_url must be either a direct company careers page or an official company ATS link such as Greenhouse, Lever, Workday, or Ashby.\n"
        "- Do NOT return job-board, recruiting-agency, visa-sponsorship directory, or aggregator links such as Indeed, LinkedIn Jobs, Wellfound, Glassdoor, TotalJobs, AI job boards, recruiter websites, or similar sites.\n"
        "- Reject generic ATS wrappers like `boards.greenhouse.io/embed/...` or `job-boards.*` unless the link is clearly company-specific and official.\n"
        "- Use the candidate CV/resume details to match role level, skills, tools, and domain experience when available.\n"
        "- Strongly prefer roles that match the user's `Want` filters.\n"
        "- Avoid roles, industries, and work styles listed in the user's `Don't want` filters.\n"
        "- If a preferred location is provided, prioritize jobs in that location or remote roles compatible with it.\n"
        "- Avoid any company-role-url combination that already appears in the tracked list.\n"
        f"- Return exactly {result_count} results when possible; if fewer real official matches exist, return only the available ones.\\n"
        "- Keep each reason short and practical.\n"
        "- Focus on roles that match the user's prompt and look currently open."
    )
    user_prompt = compose_search_preferences(prompt, want, dont_want, location, cv_text) + f"\n\nAlready tracked items to avoid repeating:\n{existing_text}"

    payload = {
        "model": model,
        "tools": [{"type": "web_search_preview"}],
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": instructions}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_recommendations",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "jobs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "company": {"type": "string"},
                                    "role": {"type": "string"},
                                    "location": {"type": "string"},
                                    "official_url": {"type": "string"},
                                    "reason": {"type": "string"},
                                    "source": {"type": "string"}
                                },
                                "required": ["company", "role", "location", "official_url", "reason", "source"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["jobs"],
                    "additionalProperties": False
                }
            }
        },
        "max_output_tokens": 1400,
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while calling OpenAI: {exc.reason}") from exc

    jobs = [
        item
        for item in parse_jobs_from_response(extract_output_text(response_payload))
        if is_official_job_url(item["official_url"], item.get("company", ""))
    ]
    if not jobs:
        raise RuntimeError("OpenAI only returned third-party job-board links. Try again with a narrower prompt.")

    unique_jobs: list[dict[str, str]] = []
    known = set(seen_keys)
    for item in jobs:
        key = normalize_job_key(item["company"], item["role"], item["official_url"])
        if key in known:
            continue
        known.add(key)
        unique_jobs.append(item)

    return unique_jobs


def generate_recommendations(
    prompt: str,
    want: str = "",
    dont_want: str = "",
    location: str = "",
    result_count: str = "5",
) -> tuple[int, str]:
    save_prompt(prompt)
    save_filters(want, dont_want, location, result_count=result_count)
    saved_cv_text = load_saved_cv_text()
    saved_filters = load_saved_filters()
    ignored_sites = {str(site).lower() for site in saved_filters.get("ignored_sites", [])}
    try:
        target_count = max(1, min(20, int(str(result_count).strip() or saved_filters.get("result_count", "5"))))
    except ValueError:
        target_count = 5
    apps = read_rows(APPLICATIONS_FILE)
    existing_recommendations = read_recommendation_rows()
    seen_keys, seen_descriptions = collect_existing_jobs(apps, existing_recommendations)

    new_jobs = fetch_ai_job_recommendations(prompt, want, dont_want, location, saved_cv_text, target_count, seen_keys, seen_descriptions)
    if ignored_sites:
        new_jobs = [job for job in new_jobs if normalize_site_host(job.get("official_url", "")) not in ignored_sites]
    if not new_jobs:
        return 0, "No new recommendations were added because the results were duplicates or from ignored sites."

    rows_to_add = []
    for item in new_jobs:
        rows_to_add.append(
            {
                "id": uuid.uuid4().hex[:12],
                "date_added": date.today().isoformat(),
                "company": item["company"],
                "role": item["role"],
                "location": item.get("location", ""),
                "official_url": item["official_url"],
                "reason": item.get("reason", ""),
                "status": "New",
                "source": item.get("source", "OpenAI web search"),
                "notes": "",
            }
        )

    write_rows(RECOMMENDATIONS_FILE, RECOMMENDATION_HEADERS, existing_recommendations + rows_to_add)
    return len(rows_to_add), f"Added {len(rows_to_add)} new recommendation(s)."


def update_recommendation_status(recommendation_id: str, new_status: str) -> str:
    clean_status = new_status.strip().title()
    if clean_status not in ALLOWED_RECOMMENDATION_STATUSES:
        raise RuntimeError(f"Unsupported status: {new_status}")

    rows = read_recommendation_rows()
    updated = False
    for row in rows:
        if row.get("id") == recommendation_id:
            row["status"] = clean_status
            updated = True
            break

    if not updated:
        raise RuntimeError("Recommendation not found.")

    write_rows(RECOMMENDATIONS_FILE, RECOMMENDATION_HEADERS, rows)
    return f"Updated recommendation to {clean_status}."


def delete_recommendation(recommendation_id: str) -> str:
    rows = read_recommendation_rows()
    remaining_rows = [row for row in rows if row.get("id") != recommendation_id]
    if len(remaining_rows) == len(rows):
        raise RuntimeError("Recommendation not found.")

    write_rows(RECOMMENDATIONS_FILE, RECOMMENDATION_HEADERS, remaining_rows)
    return "Deleted recommendation from the feed."


def delete_filter_tag(kind: str, tag_value: str) -> str:
    filters = load_saved_filters()
    want_tags = list(filters.get("want_tags", []))
    dont_want_tags = list(filters.get("dont_want_tags", []))
    ignored_sites = list(filters.get("ignored_sites", []))
    location = str(filters.get("location", ""))
    target = tag_value.strip().lower()

    if kind == "want":
        new_want_tags = [tag for tag in want_tags if tag.lower() != target]
        if len(new_want_tags) == len(want_tags):
            raise RuntimeError("Tag not found.")
        save_filters(new_want_tags, dont_want_tags, location, ignored_sites)
    elif kind == "dont_want":
        new_dont_want_tags = [tag for tag in dont_want_tags if tag.lower() != target]
        if len(new_dont_want_tags) == len(dont_want_tags):
            raise RuntimeError("Tag not found.")
        save_filters(want_tags, new_dont_want_tags, location, ignored_sites)
    elif kind == "ignored_site":
        new_ignored_sites = [site for site in ignored_sites if site.lower() != target]
        if len(new_ignored_sites) == len(ignored_sites):
            raise RuntimeError("Ignored site not found.")
        save_filters(want_tags, dont_want_tags, location, new_ignored_sites)
    else:
        raise RuntimeError("Unsupported filter tag type.")

    return f"Removed {kind.replace('_', ' ')} tag: {tag_value.strip()}"


def ignore_recommendation_site(recommendation_id: str) -> str:
    rows = read_recommendation_rows()
    target_row = next((row for row in rows if row.get("id") == recommendation_id), None)
    if not target_row:
        raise RuntimeError("Recommendation not found.")

    site_host = normalize_site_host(target_row.get("official_url", ""))
    if not site_host:
        raise RuntimeError("This recommendation does not have a website to ignore.")

    filters = load_saved_filters()
    want_tags = list(filters.get("want_tags", []))
    dont_want_tags = list(filters.get("dont_want_tags", []))
    location = str(filters.get("location", ""))
    ignored_sites = list(filters.get("ignored_sites", []))
    if site_host.lower() not in {site.lower() for site in ignored_sites}:
        ignored_sites.append(site_host)

    save_filters(want_tags, dont_want_tags, location, ignored_sites)
    remaining_rows = [row for row in rows if normalize_site_host(row.get("official_url", "")) != site_host]
    removed_count = len(rows) - len(remaining_rows)
    write_rows(RECOMMENDATIONS_FILE, RECOMMENDATION_HEADERS, remaining_rows)
    return f"Ignoring jobs from {site_host}. Removed {removed_count} recommendation(s)."


def edit_recommendation(
    recommendation_id: str,
    company: str,
    role: str,
    location: str,
    notes: str,
    new_status: str,
) -> str:
    clean_status = (new_status or "Applied").strip().title()
    if clean_status not in ALLOWED_RECOMMENDATION_STATUSES:
        raise RuntimeError(f"Unsupported status: {new_status}")

    rows = read_recommendation_rows()
    updated_row: dict[str, str] | None = None
    for row in rows:
        if row.get("id") == recommendation_id:
            row["company"] = company.strip() or row.get("company", "")
            row["role"] = role.strip() or row.get("role", "")
            row["location"] = location.strip()
            row["notes"] = notes.strip()
            row["status"] = clean_status
            updated_row = row
            break

    if not updated_row:
        raise RuntimeError("Recommendation not found.")

    write_rows(RECOMMENDATIONS_FILE, RECOMMENDATION_HEADERS, rows)
    return f"Saved changes for {updated_row.get('company', 'item')} / {updated_row.get('role', '')}."


def normalize_tab(tab: str | None) -> str:
    return tab if tab in TAB_ORDER else "feed"


def get_rows_for_tab(rows: list[dict[str, str]], active_tab: str) -> list[dict[str, str]]:
    statuses = TAB_STATUSES.get(active_tab)
    if statuses is None:
        return rows
    return [row for row in rows if (row.get("status") or "New") in statuses]


def render_tab_links(rows: list[dict[str, str]], active_tab: str) -> str:
    status_counts = Counter((row.get("status") or "New").strip() or "New" for row in rows)
    links: list[str] = []

    for tab in TAB_ORDER:
        statuses = TAB_STATUSES[tab]
        count = len(rows) if statuses is None else sum(status_counts.get(status, 0) for status in statuses)
        label = f"{TAB_LABELS[tab]} ({count})"
        class_name = "tab-link active" if tab == active_tab else "tab-link"
        links.append(f'<a class="{class_name}" href="/?tab={tab}#recommendations-section">{html.escape(label)}</a>')

    return "".join(links)


def render_applied_edit_form(row: dict[str, str], active_tab: str) -> str:
    status = row.get("status", "New") or "New"
    if status != "Applied":
        return ""

    rec_id = html.escape(row.get("id", ""), quote=True)
    company = html.escape(row.get("company", ""), quote=True)
    role = html.escape(row.get("role", ""), quote=True)
    location = html.escape(row.get("location", ""), quote=True)
    notes = html.escape(row.get("notes", ""))
    tab = html.escape(active_tab, quote=True)

    options = []
    for option in ["Applied", "Apply Later", "Not Interested", "New"]:
        selected = " selected" if option == status else ""
        options.append(f'<option value="{html.escape(option, quote=True)}"{selected}>{html.escape(option)}</option>')

    return f"""
    <details class=\"edit-box\">
      <summary>Edit applied entry</summary>
      <form method=\"post\" action=\"/edit-recommendation\" class=\"edit-form\">
        <input type=\"hidden\" name=\"id\" value=\"{rec_id}\">
        <input type=\"hidden\" name=\"tab\" value=\"{tab}\">
        <div class=\"edit-grid\">
          <div>
            <label>Company</label>
            <input type=\"text\" name=\"company\" value=\"{company}\">
          </div>
          <div>
            <label>Role</label>
            <input type=\"text\" name=\"role\" value=\"{role}\">
          </div>
          <div>
            <label>Location</label>
            <input type=\"text\" name=\"location\" value=\"{location}\">
          </div>
          <div>
            <label>Status</label>
            <select name=\"status\">{''.join(options)}</select>
          </div>
          <div class=\"full-width\">
            <label>Notes</label>
            <textarea name=\"notes\" placeholder=\"Add interview, application, or follow-up notes...\">{notes}</textarea>
          </div>
        </div>
        <div class=\"actions\">
          <button type=\"submit\">Save Applied Edit</button>
        </div>
      </form>
    </details>
    """


def render_filter_tags(kind: str, tags: list[str], active_tab: str) -> str:
    if not tags:
        if kind == "want":
            label = "want"
        elif kind == "dont_want":
            label = "don't want"
        else:
            label = "ignored site"
        return f'<div class="tiny muted">No saved {html.escape(label)} tags yet.</div>'

    forms: list[str] = []
    tab = html.escape(active_tab, quote=True)
    chip_class = "tag-chip tag-chip-warn" if kind in {"dont_want", "ignored_site"} else "tag-chip"
    for tag in tags:
        safe_tag = html.escape(tag)
        safe_value = html.escape(tag, quote=True)
        forms.append(
            f'<form method="post" action="/delete-filter-tag" class="tag-item-form">'
            f'<input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="tag" value="{safe_value}">'
            f'<input type="hidden" name="tab" value="{tab}">'
            f'<input type="hidden" name="anchor" value="ai-search-section">'
            f'<button type="submit" class="{chip_class}" title="Remove tag">{safe_tag} ×</button>'
            f'</form>'
        )
    return "".join(forms)


def render_recommendation_table(rows: list[dict[str, str]], active_tab: str) -> str:
    visible_rows = get_rows_for_tab(rows, active_tab)
    if not visible_rows:
        return f'<div class="empty">No {html.escape(TAB_LABELS[active_tab].lower())} recommendations yet.</div>'

    ordered_rows = sorted(visible_rows, key=lambda row: row.get("date_added", ""), reverse=True)
    table_rows: list[str] = []
    for row in ordered_rows:
        rec_id = html.escape(row.get("id", ""), quote=True)
        status = row.get("status", "New") or "New"
        status_class = re.sub(r"[^a-z]+", "-", status.lower()).strip("-")
        link = html.escape(row.get("official_url", ""), quote=True)
        link_html = f'<a href="{link}" target="_blank" rel="noreferrer">Official posting</a>' if link else "—"
        reason = html.escape(row.get("reason", "") or "—")
        source = html.escape(row.get("source", "") or "OpenAI web search")
        notes = (row.get("notes", "") or "").strip()
        notes_html = f'<div class="tiny">Notes: {html.escape(notes)}</div>' if notes else ""
        site_host = html.escape(normalize_site_host(row.get("official_url", "")) or "site", quote=True)
        tab = html.escape(active_tab, quote=True)
        delete_form = ""
        if active_tab in {"feed", "applied"}:
            delete_form = f"""
            <form method=\"post\" action=\"/delete-recommendation\" class=\"delete-form\">
              <input type=\"hidden\" name=\"id\" value=\"{rec_id}\">
              <input type=\"hidden\" name=\"tab\" value=\"{tab}\">
              <input type=\"hidden\" name=\"anchor\" value=\"recommendations-section\">
              <button type=\"submit\" class=\"danger-button\">Delete</button>
            </form>
            """

        action_form = f"""
        <form method=\"post\" action=\"/update-recommendation-status\" class=\"status-form\">
          <input type=\"hidden\" name=\"id\" value=\"{rec_id}\">
          <input type=\"hidden\" name=\"tab\" value=\"{tab}\">
          <input type=\"hidden\" name=\"anchor\" value=\"recommendations-section\">
          <button type=\"submit\" name=\"status\" value=\"Applied\">Applied</button>
          <button type=\"submit\" name=\"status\" value=\"Apply Later\">Apply Later</button>
          <button type=\"submit\" name=\"status\" value=\"Not Interested\">Not Interested</button>
        </form>
        <form method=\"post\" action=\"/ignore-recommendation-site\" class=\"delete-form\">
          <input type=\"hidden\" name=\"id\" value=\"{rec_id}\">
          <input type=\"hidden\" name=\"tab\" value=\"{tab}\">
          <input type=\"hidden\" name=\"anchor\" value=\"ai-search-section\">
          <button type=\"submit\" title=\"Ignore future jobs from {site_host}\">Ignore Site</button>
        </form>
        {delete_form}
        {render_applied_edit_form(row, active_tab)}
        """

        table_rows.append(
            "<tr>"
            f"<td>{html.escape(row.get('date_added', '') or '—')}</td>"
            f"<td><div class=\"company-name\">{html.escape(row.get('company', '') or '—')}</div></td>"
            f"<td>{html.escape(row.get('role', '') or '—')}</td>"
            f"<td>{html.escape(row.get('location', '') or '—')}</td>"
            f"<td>{link_html}<div class=\"tiny muted\">{source}</div></td>"
            f"<td>{reason}{notes_html}</td>"
            f"<td><span class=\"status-chip status-{status_class}\">{html.escape(status)}</span></td>"
            f"<td>{action_form}</td>"
            "</tr>"
        )

    return (
        "<table><thead><tr>"
        "<th>Added</th><th>Company</th><th>Role</th><th>Location</th><th>Official Site</th><th>Why It Matches</th><th>Status</th><th>Update</th>"
        f"</tr></thead><tbody>{''.join(table_rows)}</tbody></table>"
    )


def render_dashboard(flash_message: str = "", active_tab: str = "feed") -> str:
    ensure_support_files()
    active_tab = normalize_tab(active_tab)
    apps = read_rows(APPLICATIONS_FILE)
    contacts = read_rows(CONTACTS_FILE)
    interviews = read_rows(INTERVIEWS_FILE)
    recommendation_rows = read_recommendation_rows()
    saved_prompt = load_saved_prompt()
    saved_cv_text = load_saved_cv_text()
    saved_filters = load_saved_filters()
    saved_want = str(saved_filters.get("want", ""))
    saved_dont_want = str(saved_filters.get("dont_want", ""))
    saved_location = str(saved_filters.get("location", ""))
    saved_result_count = str(saved_filters.get("result_count", "5"))
    saved_want_tags = list(saved_filters.get("want_tags", []))
    saved_dont_want_tags = list(saved_filters.get("dont_want_tags", []))
    saved_ignored_sites = list(saved_filters.get("ignored_sites", []))

    today = date.today()
    soon = today + timedelta(days=7)

    status_counts = Counter((row.get("status") or "Unknown").strip() or "Unknown" for row in apps)
    status_items = "".join(
        f'<span class="pill">{html.escape(status)}: {count}</span>' for status, count in sorted(status_counts.items())
    ) or '<span class="muted">No applications yet</span>'

    recent_apps = sorted(
        apps,
        key=lambda row: parse_date(row.get("date_applied")) or date.min,
        reverse=True,
    )[:10]

    due_followups = sorted(
        [row for row in apps if (follow_up := parse_date(row.get("follow_up_date"))) and today <= follow_up <= soon],
        key=lambda row: parse_date(row.get("follow_up_date")) or date.max,
    )

    upcoming_interviews = sorted(
        [row for row in interviews if (interview_day := parse_date(row.get("interview_date"))) and today <= interview_day <= soon],
        key=lambda row: parse_date(row.get("interview_date")) or date.max,
    )

    flash_html = f'<div class="flash">{html.escape(flash_message)}</div>' if flash_message else ""
    api_status = html.escape(get_api_status_message())
    tab_links = render_tab_links(recommendation_rows, active_tab)
    recommendation_table = render_recommendation_table(recommendation_rows, active_tab)
    want_tags_html = render_filter_tags("want", saved_want_tags, active_tab)
    dont_want_tags_html = render_filter_tags("dont_want", saved_dont_want_tags, active_tab)
    ignored_sites_html = render_filter_tags("ignored_site", saved_ignored_sites, active_tab)
    cv_status = (
        f"Saved CV loaded for matching ({len(saved_cv_text.split())} words)."
        if saved_cv_text
        else "No CV saved yet. Upload a PDF/DOCX or paste your resume below so job search can use it."
    )
    filter_pills = []
    if saved_want:
        filter_pills.append(f'<span class="pill">Want: {html.escape(saved_want)}</span>')
    if saved_dont_want:
        filter_pills.append(f'<span class="pill pill-warn">Don\'t want: {html.escape(saved_dont_want)}</span>')
    if saved_location:
        filter_pills.append(f'<span class="pill">Location: {html.escape(saved_location)}</span>')
    filter_summary = "".join(filter_pills) or '<span class="muted">No saved filters yet.</span>'

    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <meta http-equiv=\"refresh\" content=\"300\">
  <title>Job Search Dashboard</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: #131a2a;
      --muted: #9fb0d0;
      --text: #ecf2ff;
      --accent: #6ea8fe;
      --accent-2: #73e2a7;
      --border: #26314d;
      --warning: #ffcf66;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, Arial, sans-serif;
      background: linear-gradient(180deg, #0b1020, #11182b);
      color: var(--text);
    }}
    .container {{ max-width: 1600px; margin: 0 auto; padding: 24px; }}
    .hero {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 20px; flex-wrap: wrap; }}
    .hero h1 {{ margin: 0 0 6px; font-size: 2rem; }}
    .hero p {{ margin: 0; color: var(--muted); }}
    .stamp {{ color: var(--muted); font-size: 0.95rem; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .card, .panel {{ background: rgba(19, 26, 42, 0.95); border: 1px solid var(--border); border-radius: 16px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2); }}
    .card {{ padding: 18px; }}
    .card .label {{ color: var(--muted); font-size: 0.92rem; }}
    .card .value {{ font-size: 2rem; font-weight: 700; margin-top: 8px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
    .grid-2 > .panel:only-child {{ grid-column: 1 / -1; }}
    .layout {{ display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 16px; }}
    .stack {{ display: grid; gap: 16px; }}
    .panel {{ padding: 16px; overflow: auto; }}
    h2 {{ margin: 0 0 12px; font-size: 1.1rem; }}
    .pills {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .pill {{ padding: 6px 10px; border-radius: 999px; background: rgba(110, 168, 254, 0.12); border: 1px solid rgba(110, 168, 254, 0.35); color: #dbe8ff; font-size: 0.9rem; }}
    .pill-warn {{ background: rgba(255, 120, 120, 0.12); border-color: rgba(255, 120, 120, 0.35); color: #ffd6d6; }}
    .muted, .empty {{ color: var(--muted); }}
    .company-name {{ font-weight: 700; color: #ffffff; font-size: 1rem; }}
    .tiny {{ font-size: 0.82rem; margin-top: 6px; }}
    ul {{ margin: 0; padding-left: 18px; }}
    li {{ margin: 8px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.95rem; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    a {{ color: var(--accent-2); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    textarea, input[type="text"], input[type="file"], select {{ width: 100%; border-radius: 10px; border: 1px solid var(--border); padding: 12px; background: #0f1526; color: var(--text); }}
    textarea {{ min-height: 120px; resize: vertical; }}
    .field-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-top: 12px; }}
    .edit-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-top: 10px; }}
    .tag-group {{ margin-top: 10px; }}
    .tag-wrap {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
    .tag-item-form {{ margin: 0; }}
    .field-actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
    .small-button {{ font-size: 0.82rem; padding: 7px 10px; }}
    .tag-chip {{ border-radius: 999px; border: 1px solid rgba(110, 168, 254, 0.35); background: rgba(110, 168, 254, 0.12); color: #dbe8ff; padding: 6px 10px; font-size: 0.84rem; }}
    .tag-chip-warn {{ border-color: rgba(255, 120, 120, 0.35); background: rgba(255, 120, 120, 0.12); color: #ffd6d6; }}
    .full-width {{ grid-column: 1 / -1; }}
    label {{ display: block; font-size: 0.9rem; color: var(--muted); margin-bottom: 6px; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
    button {{ border: 0; border-radius: 10px; padding: 9px 12px; cursor: pointer; font-weight: 600; background: #21304f; color: var(--text); }}
    button:hover {{ filter: brightness(1.08); }}
    .primary {{ background: linear-gradient(135deg, #3b82f6, #2563eb); }}
    .notice, .flash {{ border-radius: 12px; padding: 10px 12px; margin-bottom: 12px; }}
    .notice {{ background: rgba(255, 207, 102, 0.1); border: 1px solid rgba(255, 207, 102, 0.35); color: #ffe7ac; }}
    .flash {{ background: rgba(115, 226, 167, 0.12); border: 1px solid rgba(115, 226, 167, 0.35); color: #cff7e4; }}
    .status-chip {{ display: inline-block; padding: 6px 10px; border-radius: 999px; font-size: 0.85rem; border: 1px solid transparent; }}
    .status-new {{ background: rgba(110, 168, 254, 0.12); border-color: rgba(110, 168, 254, 0.35); }}
    .status-applied {{ background: rgba(115, 226, 167, 0.12); border-color: rgba(115, 226, 167, 0.35); }}
    .status-apply-later {{ background: rgba(255, 207, 102, 0.12); border-color: rgba(255, 207, 102, 0.35); }}
    .status-not-interested {{ background: rgba(255, 120, 120, 0.12); border-color: rgba(255, 120, 120, 0.35); }}
    .status-form {{ display: flex; gap: 6px; flex-wrap: wrap; min-width: 220px; margin-bottom: 8px; }}
    .delete-form {{ margin-bottom: 8px; }}
    .status-form button {{ font-size: 0.82rem; padding: 7px 9px; }}
    .danger-button {{ background: linear-gradient(135deg, #dc2626, #b91c1c); }}
    .tab-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0 16px; }}
    .tab-link {{ display: inline-block; padding: 8px 12px; border-radius: 999px; text-decoration: none; background: rgba(110, 168, 254, 0.08); border: 1px solid rgba(110, 168, 254, 0.22); color: var(--text); }}
    .tab-link.active {{ background: linear-gradient(135deg, #3b82f6, #2563eb); border-color: transparent; }}
    .edit-box {{ margin-top: 8px; border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; background: rgba(11, 16, 32, 0.45); }}
    .edit-box summary {{ cursor: pointer; color: var(--accent-2); }}
    @media (max-width: 1100px) {{
      .grid-2, .layout, .field-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class=\"container\">
    <div class=\"hero\">
      <div>
        <h1>Job Search Dashboard</h1>
        <p>Track applications, networking, interviews, and AI job recommendations in one place.</p>
      </div>
      <div class=\"stamp\">Updated {today.isoformat()}</div>
    </div>

    {flash_html}

    <section class=\"cards\">
      <div class=\"card\"><div class=\"label\">Applications</div><div class=\"value\">{len(apps)}</div></div>
      <div class=\"card\"><div class=\"label\">Contacts</div><div class=\"value\">{len(contacts)}</div></div>
      <div class=\"card\"><div class=\"label\">Interviews</div><div class=\"value\">{len(interviews)}</div></div>
      <div class=\"card\"><div class=\"label\">Saved AI Recommendations</div><div class=\"value\">{len(recommendation_rows)}</div></div>
    </section>

    <section class=\"grid-2\">
      <section class=\"panel\" id=\"ai-search-section\">
        <h2>AI Job Search</h2>
        <div class=\"notice\">{api_status}</div>
        <p class=\"muted\">Save a reusable prompt, then search online for official job postings. Existing tracked jobs are filtered to avoid repeat recommendations.</p>
        <form method=\"post\" action=\"/generate-recommendations\">
          <input type=\"hidden\" name=\"tab\" value=\"{html.escape(active_tab, quote=True)}\">
          <textarea name=\"prompt\" placeholder=\"Describe the roles you want...\">{html.escape(saved_prompt)}</textarea>
          <div class=\"field-grid\">
            <div>
              <label for=\"want\">Want</label>
              <input id=\"want\" type=\"text\" name=\"want\" value=\"{html.escape(saved_want, quote=True)}\" placeholder=\"backend, AI, remote, startups\">
              <div class=\"field-actions\">
                <button type=\"submit\" formaction=\"/save-prompt\" name=\"anchor\" value=\"ai-search-section\" class=\"small-button\">Add Want Tag</button>
              </div>
            </div>
            <div>
              <label for=\"dont_want\">Don't want</label>
              <input id=\"dont_want\" type=\"text\" name=\"dont_want\" value=\"{html.escape(saved_dont_want, quote=True)}\" placeholder=\"onsite-only, sales, support roles\">
              <div class=\"field-actions\">
                <button type=\"submit\" formaction=\"/save-prompt\" name=\"anchor\" value=\"ai-search-section\" class=\"small-button\">Add Don't Want Tag</button>
              </div>
            </div>
            <div>
              <label for=\"location\">Location</label>
              <input id=\"location\" type=\"text\" name=\"location\" value=\"{html.escape(saved_location, quote=True)}\" placeholder=\"Seattle, Remote US, New York\">
            </div>
            <div>
              <label for=\"result_count\">Results</label>
              <input id=\"result_count\" type=\"text\" name=\"result_count\" value=\"{html.escape(saved_result_count, quote=True)}\" placeholder=\"5\">
            </div>
          </div>
          <div class=\"tiny muted\">Type tags in `Want` or `Don't want`, then use the add button or save button. Remove a tag by pressing its `×` chip below.</div>
          <div class=\"actions\">
            <button type=\"submit\" formaction=\"/save-prompt\" name=\"anchor\" value=\"ai-search-section\">Save Prompt + Filters</button>
            <button type=\"submit\" class=\"primary\" name=\"anchor\" value=\"recommendations-section\">Search Online for Jobs</button>
          </div>
        </form>
        <div class=\"tag-group\">
          <div class=\"tiny muted\">Want tags</div>
          <div class=\"tag-wrap\">{want_tags_html}</div>
        </div>
        <div class=\"tag-group\">
          <div class=\"tiny muted\">Don't want tags</div>
          <div class=\"tag-wrap\">{dont_want_tags_html}</div>
        </div>
        <div class=\"tag-group\">
          <div class=\"tiny muted\">Ignored job websites</div>
          <div class=\"tag-wrap\">{ignored_sites_html}</div>
        </div>
        <div class=\"pills\" style=\"margin-top: 12px;\">{filter_summary}</div>

        <details class=\"edit-box\" id=\"cv-section\" style=\"margin-top: 12px;\" open>
          <summary>CV / Resume for job matching</summary>
          <div class=\"tiny muted\">{html.escape(cv_status)}</div>
          <form method=\"post\" action=\"/upload-cv\" enctype=\"multipart/form-data\" class=\"edit-form\">
            <input type=\"hidden\" name=\"tab\" value=\"{html.escape(active_tab, quote=True)}\">
            <input type=\"hidden\" name=\"anchor\" value=\"cv-section\">
            <div class=\"full-width\" style=\"margin-top: 8px;\">
              <label for=\"cv_file\">Upload PDF or DOCX CV</label>
              <input id=\"cv_file\" type=\"file\" name=\"cv_file\" accept=\".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document\">
              <div class=\"tiny muted\">Upload a resume file and the dashboard will extract the text for job matching.</div>
            </div>
            <div class=\"actions\">
              <button type=\"submit\" class=\"primary\">Upload PDF / DOCX</button>
            </div>
          </form>
          <form method=\"post\" action=\"/save-cv\" class=\"edit-form\">
            <input type=\"hidden\" name=\"tab\" value=\"{html.escape(active_tab, quote=True)}\">
            <input type=\"hidden\" name=\"anchor\" value=\"cv-section\">
            <div class=\"full-width\" style=\"margin-top: 8px;\">
              <label for=\"cv_text\">Paste or edit your CV / resume text</label>
              <textarea id=\"cv_text\" name=\"cv_text\" placeholder=\"Paste your resume here so the job search can match your skills, experience, and keywords...\">{html.escape(saved_cv_text)}</textarea>
            </div>
            <div class=\"actions\">
              <button type=\"submit\">Save CV / Resume Text</button>
            </div>
          </form>
        </details>

        <p class=\"tiny muted\">Tip: if your API key lives in another repo, launch with `JOB_SEARCH_SECRET_FILE=/path/to/that/.secret`.</p>
      </section>

    </section>

    <section class=\"panel\" id=\"recommendations-section\" style=\"margin-bottom: 16px;\">
      <h2>AI Recommendations List</h2>
      <div class=\"tab-row\">{tab_links}</div>
      {recommendation_table}
    </section>

    <div class=\"layout\">
      <div class=\"stack\">
        <section class=\"panel\">
          <h2>Status Breakdown</h2>
          <div class=\"pills\">{status_items}</div>
        </section>

        <section class=\"panel\">
          <h2>Recent Applications</h2>
          {render_table([
              ("date_applied", "Date"),
              ("company", "Company"),
              ("role", "Role"),
              ("status", "Status"),
              ("follow_up_date", "Follow Up"),
          ], recent_apps, "No applications logged yet.")}
        </section>

        <section class=\"panel\">
          <h2>Contacts</h2>
          {render_table([
              ("name", "Name"),
              ("company", "Company"),
              ("relationship", "Relationship"),
              ("next_follow_up", "Next Follow Up"),
          ], contacts[:10], "No contacts logged yet.")}
        </section>
      </div>

      <div class=\"stack\">
        <section class=\"panel\">
          <h2>Follow-ups Due Soon</h2>
          {render_table([
              ("company", "Company"),
              ("role", "Role"),
              ("follow_up_date", "Follow Up"),
              ("notes", "Notes"),
          ], due_followups, "No follow-ups due in the next 7 days.")}
        </section>

        <section class=\"panel\">
          <h2>Upcoming Interviews</h2>
          {render_table([
              ("company", "Company"),
              ("role", "Role"),
              ("stage", "Stage"),
              ("interview_date", "Date"),
          ], upcoming_interviews, "No upcoming interviews in the next 7 days.")}
        </section>
      </div>
    </div>
  </div>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def _read_form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(raw)
        return {key: values[0] for key, values in parsed.items()}

    def _read_form_submission(self) -> tuple[dict[str, str], dict[str, dict[str, str | bytes]]]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw_body
            )
            fields: dict[str, str] = {}
            files: dict[str, dict[str, str | bytes]] = {}
            for part in message.iter_parts():
                if part.get_content_disposition() != "form-data":
                    continue
                name = part.get_param("name", header="content-disposition") or ""
                filename = part.get_filename()
                payload = part.get_payload(decode=True) or b""
                if filename:
                    files[name] = {
                        "filename": os.path.basename(filename),
                        "content": payload,
                    }
                else:
                    charset = part.get_content_charset() or "utf-8"
                    fields[name] = payload.decode(charset, errors="ignore")
            return fields, files

        return self._read_form_data(), {}

    def _redirect_with_message(self, message: str, tab: str = "feed", anchor: str = "recommendations-section") -> None:
        params = [f"tab={urllib.parse.quote(normalize_tab(tab))}"]
        if message:
            params.append("flash=" + urllib.parse.quote(message))
        location = "/?" + "&".join(params)
        if anchor:
            location += "#" + urllib.parse.quote(anchor)
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/", "/index.html"}:
            self.send_error(404, "Not Found")
            return

        query = urllib.parse.parse_qs(parsed.query)
        flash_message = query.get("flash", [""])[0]
        active_tab = normalize_tab(query.get("tab", ["feed"])[0])
        page = render_dashboard(flash_message, active_tab).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        form, files = self._read_form_submission()
        target_tab = form.get("tab", "feed")
        target_anchor = form.get("anchor", "recommendations-section")

        try:
            if parsed.path == "/upload-cv":
                upload = files.get("cv_file", {})
                filename = str(upload.get("filename", ""))
                content = upload.get("content", b"")
                if not isinstance(content, bytes):
                    content = str(content).encode("utf-8", errors="ignore")
                extracted_text = extract_text_from_uploaded_cv(filename, content)
                save_cv_text(extracted_text)
                word_count = len(extracted_text.split())
                self._redirect_with_message(
                    f"Uploaded {filename} and saved {word_count} words for matching.",
                    target_tab,
                    target_anchor,
                )
                return

            if parsed.path == "/save-cv":
                save_cv_text(form.get("cv_text", ""))
                self._redirect_with_message("Saved CV / resume for job matching.", target_tab, target_anchor)
                return

            if parsed.path == "/save-prompt":
                save_prompt(form.get("prompt", ""))
                save_filters(
                    form.get("want", ""),
                    form.get("dont_want", ""),
                    form.get("location", ""),
                    result_count=form.get("result_count", "5"),
                )
                self._redirect_with_message("Saved the AI job-search prompt and filters.", target_tab, target_anchor)
                return

            if parsed.path == "/generate-recommendations":
                _, message = generate_recommendations(
                    form.get("prompt", ""),
                    form.get("want", ""),
                    form.get("dont_want", ""),
                    form.get("location", ""),
                    form.get("result_count", "5"),
                )
                self._redirect_with_message(message, target_tab, target_anchor)
                return

            if parsed.path == "/update-recommendation-status":
                message = update_recommendation_status(form.get("id", ""), form.get("status", ""))
                self._redirect_with_message(message, target_tab, target_anchor)
                return

            if parsed.path == "/ignore-recommendation-site":
                message = ignore_recommendation_site(form.get("id", ""))
                self._redirect_with_message(message, target_tab, target_anchor)
                return

            if parsed.path == "/delete-recommendation":
                message = delete_recommendation(form.get("id", ""))
                self._redirect_with_message(message, target_tab, target_anchor)
                return

            if parsed.path == "/delete-filter-tag":
                message = delete_filter_tag(form.get("kind", ""), form.get("tag", ""))
                self._redirect_with_message(message, target_tab, target_anchor)
                return

            if parsed.path == "/edit-recommendation":
                message = edit_recommendation(
                    form.get("id", ""),
                    form.get("company", ""),
                    form.get("role", ""),
                    form.get("location", ""),
                    form.get("notes", ""),
                    form.get("status", "Applied"),
                )
                self._redirect_with_message(message, target_tab, target_anchor)
                return

            self.send_error(404, "Not Found")
        except Exception as exc:
            self._redirect_with_message(str(exc), target_tab, target_anchor)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    ensure_support_files()

    parser = argparse.ArgumentParser(description="Run the local job search dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind the dashboard server to.")
    parser.add_argument("--port", type=int, default=8000, help="Port to serve the dashboard on.")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
