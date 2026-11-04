"""Tailor resumes for dashboard jobs in 'Make resume'.
  make_resumes.py --url URL   one job (used by the web app worker). Exit 0 = delivered; 1 = not delivered (last stdout line says why)
  make_resumes.py             every queued job
"""
import fcntl, json, re, sqlite3, sys, time, traceback
from datetime import date
from pathlib import Path
sys.path.insert(0, "/home/rohan/claude-chats/apply-pilot/apply-pilot-Updated-main")
from applypilot.config import RESUME_PATH, TAILORED_DIR, load_env, load_profile
load_env()
from applypilot.scoring.tailor import tailor_resume
from applypilot.scoring.pdf import convert_to_pdf

DB = "/home/rohan/.applypilot/applypilot.db"
slug = lambda s: re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:40]

def company(j):
    import urllib.parse
    a = urllib.parse.unquote(urllib.parse.unquote(j["application_url"] or ""))
    m = re.search(r"-at-([a-z0-9\-]+?)-\d{8,}", a)
    if m: return m.group(1).replace("-", " ").title()
    m = re.search(r"naukri\.com/job-listings-.*?-([a-z0-9]+)-(?:bengaluru|hyderabad|mumbai|pune|chennai|delhi|remote)", j["url"] or "")
    return m.group(1).title() if m else (j["site"] or "Company").title()

def next_no():
    nums = [int(m.group(1)) for f in Path(TAILORED_DIR).glob("*.pdf") if (m := re.match(r"(\d{2,3})_", f.name))]
    return max(nums, default=0) + 1

def process(c, j, resume, profile):
    """Returns (ok, message)."""
    if not (j.get("full_description") or "").strip(): return False, "This job has no description to tailor against."
    text, rep = tailor_resume(resume, j, profile)
    if rep["status"] not in ("approved", "approved_with_judge_warning"):
        return False, f"The resume failed its accuracy checks ({rep['status']}). Tap Retry."
    with open("/tmp/applypilot-pdf.lock", "w") as lk:      # only numbering + PDF render are serialized; model calls run in parallel
        fcntl.flock(lk, fcntl.LOCK_EX)
        name = f"{next_no():02d}_{slug(company(j))}_{slug(j['title'])}_{re.sub(r'[^0-9]', '', j['url'])[-10:] or 'x'}"
        txt = Path(TAILORED_DIR) / f"{name}.txt"; txt.write_text(text, encoding="utf-8")
        pdf = convert_to_pdf(txt)
    lines = [l for l in text.splitlines() if l.strip()]
    changes = [f"Tagline: {lines[1]}" if len(lines) > 1 else "Tailored headline",
               "Bullets reordered and reworded for this posting. Same facts and numbers; education, certifications and dates unchanged."]
    if rep["status"] == "approved_with_judge_warning": changes.append("Checker flagged a minor wording concern: skim before sending.")
    c.execute("UPDATE ui_state SET decision='Ready to apply', resume_name=?, resume_made=?, changes=?, work_status='', work_msg='', updated_at=datetime('now') WHERE url=?",
              (pdf.name, date.today().isoformat(), json.dumps(changes), j["url"])); c.commit()
    return True, pdf.name

def main():
    c = sqlite3.connect(DB, timeout=60); c.row_factory = sqlite3.Row
    url = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else None
    q = "SELECT j.* FROM ui_state u JOIN jobs j ON j.url=u.url WHERE u.decision='Make resume'" + (" AND j.url=?" if url else "") + " ORDER BY j.fit_score DESC"
    rows = [dict(r) for r in c.execute(q, (url,) if url else ())]
    if not rows: print("Nothing to do (job is no longer in Make resume)."); sys.exit(0)
    resume, profile = RESUME_PATH.read_text(encoding="utf-8"), load_profile()
    bad = 0
    for j in rows:
        st = c.execute("SELECT decision FROM ui_state WHERE url=?", (j["url"],)).fetchone()
        if not st or st["decision"] != "Make resume": continue          # moved or already done while waiting
        t0 = time.time()
        try: ok, msg = process(c, j, resume, profile)
        except Exception as e:
            traceback.print_exc(); ok, msg = False, f"Error: {repr(e)[:200]}"
        print(("OK " if ok else "FAIL ") + msg + f" ({time.time()-t0:.0f}s)", flush=True)
        if not ok:
            bad += 1
            c.execute("UPDATE ui_state SET work_status='failed', work_msg=? WHERE url=?", (msg, j["url"])); c.commit()
    sys.exit(1 if bad else 0)

if __name__ == "__main__": main()
