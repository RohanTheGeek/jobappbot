"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import GeminiQuotaExhausted, get_client_for

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

CANDIDATE TARGETS (apply these strictly):
- Wanted roles: Product Analyst, Associate Product Manager / Product Manager I, AI workflow automation, AI automation engineer, applied AI / agentic AI / LLM engineer, AI solutions engineer, solutions engineer or architect, technical product manager, business analyst, customer success or implementation engineer in a tech product.
- ROLE FIELD: the job must be a product, analytics, AI/tech, solutions or business-analysis role at a technology-oriented employer. Finance, accounting, audit, tax, legal, HR, recruiting, sales, marketing, supply chain, manufacturing, pure operations/admin, clinical/medical, and non-tech roles must score 3 or lower, even if the candidate has loosely related skills.
- SENIORITY: the candidate has about 2 years of experience. If the posting asks for 5 or more years, or the title is Senior/Lead/Staff/Principal/Manager-level (including levels like II, III), score 3 or lower. If it asks for 3-4 years, score at most 5. Entry-level, associate, graduate and 0-3 year roles are the sweet spot.

- UAE JOBS (location in Dubai, Abu Dhabi or elsewhere in the UAE, onsite or remote, are acceptable): the candidate needs employer visa sponsorship, like almost every applicant. Do not penalize for visa, residence or NOC requirements at all. Judge role field and seniority exactly as for India.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client_for("LLM_SCORE_MODEL")
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except GeminiQuotaExhausted:
        raise
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


_YEARS_PATTERNS = (
    re.compile(r"(?<![\d.])(\d{1,2})\s*\+?\s*(?:to|-|\u2013|\u2014)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?<![\d.])(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?:minimum|min\.?|at\s*least)\s*(?:of\s*)?(?<![\d.])(\d{1,2})\s*(?:years?|yrs?)", re.I),
    re.compile(r"(?<![\d.])(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s*)?(?:relevant\s*|professional\s*|hands-on\s*|total\s*)?(?:experience|exp\b)", re.I),
)


def min_years_required(title: str | None, url: str | None, description: str | None) -> int | None:
    """Smallest years-of-experience figure stated in the posting (None if none found).

    Using the smallest figure keeps this conservative: a posting that also says "2+ years" is not flagged.
    Naukri URLs carry the range too ("...-9-to-14-years-...").
    """
    text = f"{title or ''} {(url or '').replace('-', ' ')} {description or ''}"
    found = []
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(text):
            n = int(m.group(1))
            if 0 < n <= 30:
                found.append(n)
    return min(found) if found else None


_PREFERRED_CITIES = ("bengaluru", "bangalore", "hyderabad")
_REMOTE_WORDS = ("remote", "work from home", "wfh", "anywhere", "hybrid - remote")


def _location_tier(location: str | None, region: str | None = None) -> tuple[int, str]:
    """1 = Bengaluru, 2 = Hyderabad, 3 = remote, 4 = UAE bucket, 9 = anything else."""
    loc = (location or "").lower()
    if (region or "").upper() == "UAE":
        return 4, "UAE"
    if any(w in loc for w in ("bengaluru", "bangalore")):
        return 1, "Bengaluru"
    if "hyderabad" in loc:
        return 2, "Hyderabad"
    if any(w in loc for w in _REMOTE_WORDS):
        return 3, "Remote"
    if not loc.strip():
        return 3, "Unknown"
    return 9, "Other city"


def _apply_years_rule(result: dict, job: dict) -> dict:
    """Hard seniority check: postings whose smallest stated experience is 5+ years score at most 3."""
    yrs = min_years_required(job.get("title"), job.get("url"), job.get("full_description"))
    if yrs is not None and yrs >= 5 and result["score"] > 3:
        result["reasoning"] = f"(Capped from {result['score']}: posting asks for {yrs}+ years.) " + result["reasoning"]
        result["score"] = 3
    return result


def _apply_location_rule(result: dict, location: str | None, region: str | None = None) -> dict:
    """Preference order: Bengaluru, then Hyderabad, then remote. Onsite elsewhere caps the score at 4."""
    tier, label = _location_tier(location, region)
    if tier == 9 and result["score"] > 4:
        result["reasoning"] = f"(Capped from {result['score']}: onsite outside Bengaluru/Hyderabad.) " + result["reasoning"]
        result["score"] = 4
    result["reasoning"] = f"[LOCATION TIER {tier}: {label}] " + result["reasoning"]
    return result


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    # Never score from the title alone: an empty or tiny description makes the score meaningless.
    before = len(jobs)
    jobs = [j for j in jobs if len((j.get("full_description") or "").strip()) >= 200]
    if len(jobs) < before:
        log.warning("Skipped %d jobs with no usable description (need 200+ chars)", before - len(jobs))
    if not jobs:
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    quota_exhausted = False
    results: list[dict] = []

    from concurrent.futures import ThreadPoolExecutor

    def _score_one(job):
        try:
            return job, score_job(resume_text, job), None
        except GeminiQuotaExhausted as exc:
            return job, None, exc

    def _flush():
        now_ = datetime.now(timezone.utc).isoformat()
        for r_ in results:
            conn.execute(
                "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
                (r_["score"], f"{r_['keywords']}\n{r_['reasoning']}", now_, r_["url"]),
            )
        conn.commit()
        results.clear()

    pool = ThreadPoolExecutor(max_workers=4)
    for job, result, exc in pool.map(_score_one, jobs):
        if exc is not None:
            completed += 1
            errors += 1
            quota_exhausted = True
            log.error("Hard quota exhausted at job %d/%d. Details: %s", completed, len(jobs), exc)
            break

        completed += 1
        if result["score"] != 0:
            result = _apply_years_rule(result, job)
            result = _apply_location_rule(result, job.get("location"), job.get("region"))

        if result["score"] == 0:
            errors += 1
            log.error(
                "[%d/%d] Scoring failed; leaving job pending: %s",
                completed, len(jobs), job.get("title", "?")[:60],
            )
            continue

        result["url"] = job["url"]
        results.append(result)
        if len(results) >= 10:
            _flush()

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    _flush()
    pool.shutdown(wait=False)

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs", completed - errors, elapsed)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": completed - errors,
        "errors": errors,
        "quota_exhausted": quota_exhausted,
        "elapsed": elapsed,
        "distribution": distribution,
    }
