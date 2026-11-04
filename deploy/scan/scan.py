"""4-hourly streaming scan. Discovery (Bengaluru, then UAE) runs in one thread while the main
loop enriches, scores and double-checks jobs as they arrive. After every pass it rewrites
state.json (live progress + stats) and pending_push.json (new shortlist-worthy jobs).
Never tailors, writes cover letters, applies or sends anything.

  python scan.py               run a scan (exits if one is already running)
  python scan.py --mark-pushed move pending_push.json ids into pushed_ids.json
  python scan.py --seed        mark everything that qualifies today as already known
"""
import copy, fcntl, hashlib, json, os, re, sqlite3, sys, threading, time, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from applypilot.config import APP_DIR, RESUME_PATH, load_env
load_env()
from applypilot.database import init_db

SCAN_DIR = APP_DIR / "scan"
STATE, PENDING, PUSHED, LOCK = (SCAN_DIR / n for n in ("state.json", "pending_push.json", "pushed_ids.json", "scan.lock"))
INTERVAL_H, HOURS_OLD = 4, 12
PACING = {"batch_size": 5, "search_pause_seconds": 60, "batch_pause_seconds": 180,
          "rate_limit_cooldown_seconds": 300, "max_total_cooldown_seconds": 3600}   # ~2.7h for ~80 searches
DISCOVERY_BUDGET_H = 3.33                    # stop starting new searches after this; next scan picks up the rest
LOG = lambda *a: print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)
BLR = "(lower(location) LIKE '%bengaluru%' OR lower(location) LIKE '%bangalore%')"
IND = "(region IS NULL OR region!='UAE')"

def jload(p, d):
    try: return json.loads(Path(p).read_text())
    except Exception: return d

def next_slot(now):
    """Next cron slot (00:00, 04:00, 08:00 ... local time) after now, as UTC ISO."""
    loc = now.astimezone()
    h = (loc.hour // INTERVAL_H + 1) * INTERVAL_H
    nxt = loc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h - loc.hour)
    return nxt.astimezone(timezone.utc).isoformat()

def make_cfg(path, bengaluru_only=False, deadline=None):
    cfg = copy.deepcopy(yaml.safe_load(open(path)))
    cfg.update(PACING)
    if deadline: cfg["deadline_epoch"] = deadline
    cfg.setdefault("defaults", {})["hours_old"] = HOURS_OLD
    cfg["sites"] = [s for s in cfg.get("sites", []) if s not in ("glassdoor", "bayt")]  # glassdoor unreadable, bayt 403
    if bengaluru_only:
        cfg["locations"] = [l for l in cfg["locations"] if l.get("label") == "Bengaluru"]
    return cfg

def company(j):
    t = j["title"] or ""
    for k in ("Oliver Wyman", "BCG X"):
        if k.lower() in t.lower(): return k
    a = urllib.parse.unquote(urllib.parse.unquote(j["application_url"] or ""))
    m = re.search(r"-at-([a-z0-9\-]+?)-\d{8,}", a)
    if m: return m.group(1).replace("-", " ").title()
    m = re.search(r"naukri\.com/job-listings-.*?-([a-z0-9]+)-(?:bengaluru|hyderabad|mumbai|pune|chennai|delhi|remote)", j["url"] or "")
    return m.group(1).title() if m else ""

def clean(s):
    r = (s or "").split("\n", 1)[-1]; r = re.sub(r"\[LOCATION TIER \d: [\w ]+\]\s*", "", r); return r.strip()[:300]

def qualifying(conn, pushed, found_at):
    docs = []
    for reg, where in (("blr", f"{IND} AND {BLR}"), ("uae", "region='UAE'")):
        q = (f"SELECT * FROM jobs WHERE {where} AND fit_score>=7 AND (years_min IS NULL OR years_min<5) "
             "AND check_score>=7 ORDER BY (fit_score+check_score) DESC")
        for j in (dict(x) for x in conn.execute(q)):
            jid = "j" + hashlib.md5(j["url"].encode()).hexdigest()[:12]
            if jid in pushed: continue
            direct = j["application_url"] if j["application_url"] and "linkedin.com/signup" not in j["application_url"] else ""
            docs.append({"id": jid, "region": reg, "title": j["title"], "company": company(j),
                "location": re.sub(r",\s*United Arab Emirates", "", j["location"] or ""), "source": (j["site"] or "").title(),
                "s1": j["fit_score"], "s2": j["check_score"], "restored": False, "years": j["years_min"] or 0,
                "why": clean(j["score_reasoning"]), "jobUrl": j["url"], "applyUrl": direct, "dup": "", "decision": "", "note": "",
                "foundAt": found_at})
    return docs

def stats(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]
    def region_block(where):
        dist = {str(s): n for s, n in conn.execute(f"SELECT fit_score, COUNT(*) FROM jobs WHERE {where} AND fit_score IS NOT NULL GROUP BY 1")}
        src = [{"site": r[0] or "?", "total": r[1], "strong": r[2] or 0, "avg": r[3] or 0} for r in conn.execute(
            f"SELECT site, COUNT(*), SUM(fit_score>=7), ROUND(AVG(fit_score),1) FROM jobs WHERE {where} GROUP BY site ORDER BY 3 DESC, 2 DESC LIMIT 12")]
        return {"total": one(f"SELECT COUNT(*) FROM jobs WHERE {where}"), "scored": one(f"SELECT COUNT(*) FROM jobs WHERE {where} AND fit_score IS NOT NULL"),
                "strong": one(f"SELECT COUNT(*) FROM jobs WHERE {where} AND fit_score>=7"),
                "unscored": one(f"SELECT COUNT(*) FROM jobs WHERE {where} AND fit_score IS NULL"), "dist": dist, "sources": src}
    return {"totalScanned": one("SELECT COUNT(*) FROM jobs"), "totalScored": one("SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"),
            "totalStrong": one("SELECT COUNT(*) FROM jobs WHERE fit_score>=7"), "blr": region_block(IND), "uae": region_block("region='UAE'")}

def mark_pushed():
    ids = set(jload(PUSHED, [])) | {d["id"] for d in jload(PENDING, [])}
    PUSHED.write_text(json.dumps(sorted(ids))); PENDING.write_text("[]")
    LOG("marked pushed; known ids:", len(ids))

BATCH, SSTATE, LAST = SCAN_DIR / "sync_batch.json", SCAN_DIR / "sync_state.json", SCAN_DIR / "last_sync.txt"

def needs_sync():
    st = jload(STATE, {})
    print(1 if (jload(PENDING, []) or st.get("updatedAt", st.get("lastScan")) != (LAST.read_text().strip() if LAST.exists() else "")) else 0)

def snapshot():
    """Freeze what is about to be copied to the dashboard (the scan keeps changing the live files)."""
    BATCH.write_text(PENDING.read_text() if PENDING.exists() else "[]"); SSTATE.write_text(STATE.read_text())

def mark_synced():
    """Called after the dashboard write succeeded: remember the batch ids and the state stamp."""
    ids = set(jload(PUSHED, [])) | {d["id"] for d in jload(BATCH, [])}
    PUSHED.write_text(json.dumps(sorted(ids)))
    PENDING.write_text(json.dumps([d for d in jload(PENDING, []) if d["id"] not in ids], ensure_ascii=False))
    st = jload(SSTATE, {}); LAST.write_text(st.get("updatedAt", st.get("lastScan", "")))
    LOG("synced; batch", len(jload(BATCH, [])), "known ids", len(ids))

def main(seed=False):
    SCAN_DIR.mkdir(exist_ok=True)
    lockf = open(LOCK, "w")
    try: fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        LOG("a scan is already running; exiting"); return
    started = datetime.now(timezone.utc); found_at = started.isoformat()
    conn = init_db(); conn.row_factory = sqlite3.Row
    prev = jload(STATE, {})
    base = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    progress = {"running": True, "phase": "starting", "startedAt": found_at, "found": 0, "enriched": 0, "scored": 0, "checked": 0, "errors": []}
    pushed = set(jload(PUSHED, []))
    pending = {d["id"]: d for d in jload(PENDING, [])}

    def write(final=False):
        c = init_db(); c.row_factory = sqlite3.Row
        now = datetime.now(timezone.utc)
        progress["found"] = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] - base
        pushed.update(jload(PUSHED, []))
        for i in [i for i in pending if i in pushed]: del pending[i]
        for d in qualifying(c, pushed, found_at):
            pending.setdefault(d["id"], d)
        PENDING.write_text(json.dumps(list(pending.values()), ensure_ascii=False))
        st = dict(prev); st.update(stats(c))
        st.update({"intervalHours": INTERVAL_H, "progress": dict(progress), "pendingShortlist": len(pending), "updatedAt": now.isoformat(), "nextScan": next_slot(now)})
        if final:
            st.update({"lastScan": now.isoformat(),
                       "scanDurationMin": round((now - started).total_seconds() / 60, 1), "newJobsLastScan": progress["found"],
                       "newShortlistLastScan": len(pending), "scans": prev.get("scans", 0) + 1})
        STATE.write_text(json.dumps(st, indent=1))

    disc_done = threading.Event()
    def discover():
        from applypilot.discovery.jobspy import run_discovery
        for name, path, blr in (("blr", APP_DIR / "searches.yaml", True), ("uae", APP_DIR / "searches_uae.yaml", False)):
            try:
                progress["phase"] = f"discovering {name}"; LOG("discover", name); LOG(run_discovery(cfg=make_cfg(path, blr, started.timestamp() + DISCOVERY_BUDGET_H * 3600)))
            except Exception as e:
                progress["errors"].append(f"discover {name}: {e}"); LOG("ERROR", progress["errors"][-1])
        disc_done.set()

    if not seed:
        # retry LinkedIn rows that were blocked/empty (max 2 retries, one per later scan; see detail.py)
        conn.execute("UPDATE jobs SET detail_scraped_at=NULL WHERE site='linkedin' AND detail_error IN ('linkedin retry 1','linkedin retry 2') "
                     "AND (full_description IS NULL OR length(full_description)<200) AND scored_at IS NULL AND discovered_at > ?",
                     ((started - timedelta(hours=48)).isoformat(),)); conn.commit()
        t = threading.Thread(target=discover, daemon=True); t.start()
        from applypilot.enrichment.detail import run_enrichment
        from applypilot.scoring.scorer import run_scoring, score_job, _apply_location_rule
        resume = RESUME_PATH.read_text()
        idle = 0
        tried = set()                      # urls already double-checked this scan (never retry in a loop)
        stalled_at = None
        hard_stop = started.timestamp() + 3.75 * 3600
        while True:
            if time.time() > hard_stop:
                progress["errors"].append("scan stopped at the 3h45m time limit"); LOG("hard time limit reached"); break
            did = 0
            c = init_db()
            n = c.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND site NOT IN ('glassdoor','google','Workopolis')").fetchone()[0]
            if n:
                progress["phase"] = "reading new jobs"
                try: run_enrichment(limit=40, workers=2); progress["enriched"] += min(n, 40); did += 1
                except Exception as e: progress["errors"].append(f"enrich: {e}")
            n = c.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND length(full_description)>=200 AND fit_score IS NULL").fetchone()[0]
            if n and n == stalled_at:
                n = 0                      # last pass scored nothing: stop retrying until new jobs arrive
            if n:
                progress["phase"] = "scoring"
                try: r = run_scoring(limit=30); progress["scored"] += r.get("scored", 0); did += 1
                except Exception as e: progress["errors"].append(f"score: {e}")
                after = c.execute("SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND length(full_description)>=200 AND fit_score IS NULL").fetchone()[0]
                stalled_at = after if after >= n else None
            rows = [dict(x) for x in c.execute("SELECT * FROM jobs WHERE fit_score>=7 AND full_description IS NOT NULL AND check_score IS NULL LIMIT 40")]
            rows = [j for j in rows if j["url"] not in tried][:20]
            tried.update(j["url"] for j in rows)
            if rows:
                progress["phase"] = "double-checking strong matches"
                os.environ["LLM_SCORE_MODEL"] = "deepseek-v4-1-flash"
                for j in rows:
                    try:
                        res = score_job(resume, j)
                        if res["score"]:
                            res = _apply_location_rule(res, j.get("location"), j.get("region"))
                            c.execute("UPDATE jobs SET check_score=?, check_reasoning=? WHERE url=?", (res["score"], res["reasoning"], j["url"])); c.commit(); progress["checked"] += 1
                    except Exception as e: progress["errors"].append(f"check: {e}")
                os.environ.pop("LLM_SCORE_MODEL", None); did += 1
            write()
            if did: idle = 0
            elif disc_done.is_set():
                idle += 1
                if idle >= 2: break
            else:
                progress["phase"] = progress["phase"] if progress["phase"].startswith("discovering") else "discovering"; time.sleep(20)
    progress.update({"running": False, "phase": "done"})
    write(final=not seed)
    LOG("done", json.dumps({k: v for k, v in jload(STATE, {}).items() if k not in ("blr", "uae")}))

if __name__ == "__main__":
    if "--needs-sync" in sys.argv: needs_sync()
    elif "--snapshot" in sys.argv: snapshot()
    elif "--mark-synced" in sys.argv: mark_synced()
    elif "--mark-pushed" in sys.argv: mark_pushed()
    elif "--seed" in sys.argv: main(seed=True); mark_pushed()
    else: main()
