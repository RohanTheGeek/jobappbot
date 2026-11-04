"""Cold-outreach email drafts for jobs marked 'reachout'.

Flow: mark a job (user_status='reachout'), find the contact manually on
LinkedIn, then run `applypilot outreach draft` with their name, position and
email. Drafts are saved as text files only. Nothing is ever sent.
"""

from __future__ import annotations

import re
from datetime import datetime

from rich.console import Console
from rich.table import Table

from applypilot.config import APP_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection
from applypilot.llm import get_client

console = Console()
OUTREACH_DIR = APP_DIR / "outreach"


def list_marked() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT rowid AS rid, title, site, location, fit_score, url FROM jobs "
        "WHERE user_status = 'reachout' ORDER BY fit_score DESC"
    ).fetchall()
    return [dict(r) if hasattr(r, "keys") else dict(zip(
        ("rid", "title", "site", "location", "fit_score", "url"), r)) for r in rows]


def mark(url_or_id: str) -> str:
    """Mark a job for outreach by rowid or URL. Returns the job title."""
    conn = get_connection()
    key = url_or_id.strip()
    row = conn.execute(
        "SELECT rowid, title, url FROM jobs WHERE url = ? OR rowid = ?",
        (key, int(key) if key.isdigit() else -1),
    ).fetchone()
    if not row:
        raise ValueError(f"No job matches {key!r}")
    conn.execute("UPDATE jobs SET user_status = 'reachout' WHERE url = ?", (row[2],))
    conn.commit()
    return row[1]


def show_marked() -> None:
    rows = list_marked()
    if not rows:
        console.print("[yellow]No jobs marked for reachout. Use: applypilot outreach mark <id>[/yellow]")
        return
    t = Table(title="Marked for reachout")
    for c in ("id", "score", "title", "location"):
        t.add_column(c)
    for r in rows:
        t.add_row(str(r["rid"]), str(r["fit_score"]), r["title"] or "", r["location"] or "")
    console.print(t)


_SYSTEM = """You write short, honest cold outreach emails from a job seeker to a hiring manager or recruiter.

Rules:
- Use ONLY facts from the resume text and metrics given. Never invent employers, numbers, tools or claims.
- Plain, warm, confident, human tone. No hype, no buzzwords, no em dashes.
- 140-190 words in the body. Short paragraphs, no bullet-heavy layout (up to 3 short bullets allowed for proof points).
- Structure: one-line hook naming the role/company -> 2-3 proof points from the resume that match what the role needs -> one sentence: based in Bangalore, India, and willing to relocate to Dubai, or work remotely if the company allows it -> soft call to action (a short call, resume attached/available on request).
- Address the contact by first name. Sign off with the candidate's name, phone, email and LinkedIn.
- Output format exactly:
Subject: <subject line>

<email body>
"""


def draft(job_ref: str, contact_name: str, contact_position: str, contact_email: str) -> tuple[str, str]:
    """Generate and save a draft. Returns (file path, email text). Marks the job 'reachout'."""
    conn = get_connection()
    key = job_ref.strip()
    job = conn.execute(
        "SELECT title, site, location, url, full_description, description FROM jobs "
        "WHERE url = ? OR rowid = ?",
        (key, int(key) if key.isdigit() else -1),
    ).fetchone()
    if not job:
        raise ValueError(f"No job matches {key!r}")
    title, site, location, url, full_desc, desc = job
    jd = (full_desc or desc or "")[:5000]

    p = load_profile()["personal"]
    metrics = ", ".join(load_profile().get("resume_facts", {}).get("real_metrics", []))
    resume = RESUME_PATH.read_text(encoding="utf-8")

    user = f"""CANDIDATE: {p['full_name']} | {p['phone']} | {p['email']} | {p['linkedin_url']}
Based in Bangalore, India. Willing to relocate to Dubai/UAE or work remotely if allowed.

RESUME:
{resume}

VERIFIED METRICS: {metrics}

JOB: {title} ({location}) {url}
JOB DESCRIPTION:
{jd}

CONTACT: {contact_name}, {contact_position}, {contact_email}

Write the email now."""

    text = get_client().chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=1024, temperature=0.6,
    ).strip()
    text = text.replace("—", ",")

    OUTREACH_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{contact_name}-{title}".lower()).strip("-")[:60]
    path = OUTREACH_DIR / f"{datetime.now():%Y%m%d-%H%M}-{slug}.txt"
    path.write_text(f"To: {contact_email}\n{text}\n", encoding="utf-8")
    conn.execute("UPDATE jobs SET user_status = 'reachout' WHERE url = ?", (url,))
    conn.commit()
    console.print(f"[green]Saved draft (NOT sent): {path}[/green]\n")
    console.print(f"To: {contact_email}\n{text}")
    return str(path), text
