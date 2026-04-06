from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
APPLICATIONS_FILE = DATA_DIR / "applications.csv"
CONTACTS_FILE = DATA_DIR / "contacts.csv"
INTERVIEWS_FILE = DATA_DIR / "interviews.csv"

APPLICATION_HEADERS = [
    "date_applied",
    "company",
    "role",
    "location",
    "status",
    "job_url",
    "referral",
    "follow_up_date",
    "notes",
]

CONTACT_HEADERS = [
    "date_added",
    "name",
    "company",
    "relationship",
    "contact_info",
    "last_contact_date",
    "next_follow_up",
    "notes",
]

INTERVIEW_HEADERS = [
    "company",
    "role",
    "stage",
    "interview_date",
    "result",
    "next_step",
    "notes",
]


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def append_row(path: Path, headers: list[str], row: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    should_write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        if should_write_header:
            writer.writeheader()
        writer.writerow(row)


def print_summary() -> None:
    apps = read_rows(APPLICATIONS_FILE)
    contacts = read_rows(CONTACTS_FILE)
    interviews = read_rows(INTERVIEWS_FILE)

    print("\n=== Job Search Summary ===")
    print(f"Applications logged: {len(apps)}")
    print(f"Contacts logged:     {len(contacts)}")
    print(f"Interviews logged:   {len(interviews)}")

    status_counts = Counter((row.get("status") or "Unknown").strip() or "Unknown" for row in apps)
    if status_counts:
        print("\nStatus breakdown:")
        for status, count in sorted(status_counts.items()):
            print(f"- {status}: {count}")

    today = date.today()
    soon = today + timedelta(days=7)

    due_followups = []
    for row in apps:
        follow_up = parse_date(row.get("follow_up_date"))
        if follow_up and today <= follow_up <= soon:
            due_followups.append((follow_up, row))

    if due_followups:
        print("\nFollow-ups due in the next 7 days:")
        for follow_up, row in sorted(due_followups, key=lambda item: item[0]):
            print(f"- {follow_up}: {row.get('company', '')} / {row.get('role', '')}")

    upcoming_interviews = []
    for row in interviews:
        interview_date = parse_date(row.get("interview_date"))
        if interview_date and today <= interview_date <= soon:
            upcoming_interviews.append((interview_date, row))

    if upcoming_interviews:
        print("\nUpcoming interviews:")
        for interview_date, row in sorted(upcoming_interviews, key=lambda item: item[0]):
            print(f"- {interview_date}: {row.get('company', '')} / {row.get('stage', '')}")

    print()


def print_recommendations() -> None:
    apps = read_rows(APPLICATIONS_FILE)
    contacts = read_rows(CONTACTS_FILE)
    interviews = read_rows(INTERVIEWS_FILE)
    status_counts = Counter((row.get("status") or "Unknown").strip() or "Unknown" for row in apps)

    recommendations: list[str] = []

    if len(apps) < 10:
        recommendations.append("Build a target list of 20–30 companies and focus on your top-fit roles first.")

    if len(apps) > 0 and status_counts.get("Interview", 0) == 0 and len(interviews) == 0:
        recommendations.append("If applications are not converting, tailor your resume more closely to each job description.")

    if status_counts.get("Rejected", 0) >= 5:
        recommendations.append("Review rejected roles for patterns in skill gaps, level mismatch, or missing keywords.")

    if len(contacts) < max(3, len(apps) // 3):
        recommendations.append("Increase networking: aim for 3–5 quality outreach messages each week.")

    has_pending = any((row.get("status") or "").strip() in {"Applied", "Follow-Up", "Interview"} for row in apps)
    has_followups = any(parse_date(row.get("follow_up_date")) for row in apps)
    if has_pending and not has_followups:
        recommendations.append("Add follow-up dates for pending applications so opportunities do not go stale.")

    if len(interviews) > 0:
        recommendations.append("Keep a short bank of STAR stories and company-specific questions ready for every interview.")

    if not recommendations:
        recommendations.append("Your tracker looks healthy. Stay consistent and review progress at the end of each week.")

    print("\n=== Recommendations ===")
    for index, item in enumerate(recommendations, start=1):
        print(f"{index}. {item}")
    print()


def add_application(args: argparse.Namespace) -> None:
    row = {
        "date_applied": args.date_applied or today_iso(),
        "company": args.company,
        "role": args.role,
        "location": args.location or "",
        "status": args.status,
        "job_url": args.url or "",
        "referral": args.referral or "",
        "follow_up_date": args.follow_up_date or "",
        "notes": args.notes or "",
    }
    append_row(APPLICATIONS_FILE, APPLICATION_HEADERS, row)
    print(f"Added application: {args.company} / {args.role}")


def add_contact(args: argparse.Namespace) -> None:
    row = {
        "date_added": args.date_added or today_iso(),
        "name": args.name,
        "company": args.company or "",
        "relationship": args.relationship or "",
        "contact_info": args.contact_info or "",
        "last_contact_date": args.last_contact_date or "",
        "next_follow_up": args.next_follow_up or "",
        "notes": args.notes or "",
    }
    append_row(CONTACTS_FILE, CONTACT_HEADERS, row)
    print(f"Added contact: {args.name}")


def add_interview(args: argparse.Namespace) -> None:
    row = {
        "company": args.company,
        "role": args.role,
        "stage": args.stage,
        "interview_date": args.interview_date,
        "result": args.result or "",
        "next_step": args.next_step or "",
        "notes": args.notes or "",
    }
    append_row(INTERVIEWS_FILE, INTERVIEW_HEADERS, row)
    print(f"Added interview: {args.company} / {args.stage}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track job applications, contacts, and interviews.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("summary", help="Show tracker summary")
    subparsers.add_parser("recommend", help="Show next-step recommendations")

    app_parser = subparsers.add_parser("add-application", help="Add a job application")
    app_parser.add_argument("--company", required=True)
    app_parser.add_argument("--role", required=True)
    app_parser.add_argument("--location")
    app_parser.add_argument("--status", default="Applied")
    app_parser.add_argument("--url")
    app_parser.add_argument("--referral")
    app_parser.add_argument("--follow-up-date")
    app_parser.add_argument("--date-applied")
    app_parser.add_argument("--notes")
    app_parser.set_defaults(func=add_application)

    contact_parser = subparsers.add_parser("add-contact", help="Add a networking contact")
    contact_parser.add_argument("--name", required=True)
    contact_parser.add_argument("--company")
    contact_parser.add_argument("--relationship")
    contact_parser.add_argument("--contact-info")
    contact_parser.add_argument("--last-contact-date")
    contact_parser.add_argument("--next-follow-up")
    contact_parser.add_argument("--date-added")
    contact_parser.add_argument("--notes")
    contact_parser.set_defaults(func=add_contact)

    interview_parser = subparsers.add_parser("add-interview", help="Add an interview record")
    interview_parser.add_argument("--company", required=True)
    interview_parser.add_argument("--role", required=True)
    interview_parser.add_argument("--stage", required=True)
    interview_parser.add_argument("--interview-date", required=True)
    interview_parser.add_argument("--result")
    interview_parser.add_argument("--next-step")
    interview_parser.add_argument("--notes")
    interview_parser.set_defaults(func=add_interview)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "summary":
        print_summary()
    elif args.command == "recommend":
        print_recommendations()
    else:
        args.func(args)


if __name__ == "__main__":
    main()
