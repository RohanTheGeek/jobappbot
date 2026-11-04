"""ApplyPilot dashboard: reads applypilot.db directly (no sync). Password-protected."""
import hashlib, json, os, re, secrets, sqlite3, time, urllib.parse
from pathlib import Path
from flask import Flask, jsonify, redirect, request, send_from_directory, session, abort, Response

BASE = Path("/home/rohan/.applypilot")
DB = BASE / "applypilot.db"
RESUMES = BASE / "tailored_resumes"
STATE = BASE / "scan" / "state.json"
CONF = BASE / "webapp" / "webapp.env"

def conf():
    d = {}
    if CONF.exists():
        for l in CONF.read_text().splitlines():
            if "=" in l and not l.startswith("#"): k, v = l.split("=", 1); d[k.strip()] = v.strip()
    return d

C = conf()
PASSWORD = C.get("APP_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("Set APP_PASSWORD in webapp/webapp.env before starting the dashboard.")
SECRET_FILE = BASE / "webapp" / ".secret"
if not SECRET_FILE.exists():
    SECRET_FILE.write_text(secrets.token_hex(32)); SECRET_FILE.chmod(0o600)

app = Flask(__name__)
app.secret_key = SECRET_FILE.read_text().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

def db():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS ui_state(
        url TEXT PRIMARY KEY, decision TEXT DEFAULT '', prev_decision TEXT DEFAULT '', note TEXT DEFAULT '',
        skip_reason TEXT DEFAULT '', company_apply_url TEXT DEFAULT '', resume_name TEXT DEFAULT '', resume_made TEXT DEFAULT '',
        changes TEXT DEFAULT '[]', reach TEXT DEFAULT '', contact TEXT DEFAULT '{}', outreach TEXT DEFAULT '{}', updated_at TEXT)""")
    have = {r[1] for r in c.execute("PRAGMA table_info(ui_state)")}
    for col in ("work_status", "work_msg", "draft_status", "draft_msg"):
        if col not in have: c.execute(f"ALTER TABLE ui_state ADD COLUMN {col} TEXT DEFAULT ''")
    return c

# ---- login with lockout -------------------------------------------------
FAILS = {}   # ip -> (count, locked_until)
def ip(): return request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr

@app.before_request
def gate():
    if request.path in ("/login", "/healthz") : return
    if not session.get("ok"):
        if request.path.startswith("/api/"): return jsonify(error="login"), 401
        return redirect("/login")

LOGIN = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>ApplyPilot</title>
<style>body{font:16px system-ui;background:#12161d;color:#e6e9ef;display:grid;place-items:center;height:100vh;margin:0}
form{background:#1a2029;padding:28px;border-radius:12px;width:min(320px,86vw);display:flex;flex-direction:column;gap:12px}
input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #2b3340;background:#12161d;color:inherit}
button{background:#7ea3ff;color:#0d1320;border:0;font-weight:600;cursor:pointer}.e{color:#f0a85e;font-size:14px}</style>
<form method=post><b>ApplyPilot</b><input type=password name=p placeholder=Password autofocus autocomplete=current-password>%s<button>Sign in</button></form>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET": return LOGIN % ""
    n, until = FAILS.get(ip(), (0, 0))
    if time.time() < until: return LOGIN % "<div class=e>Too many tries. Wait 15 minutes.</div>", 429
    if secrets.compare_digest(request.form.get("p", ""), PASSWORD):
        FAILS.pop(ip(), None); session.permanent = True; session["ok"] = True; return redirect("/")
    n += 1; FAILS[ip()] = (n, time.time() + 900 if n >= 5 else 0)
    time.sleep(1)
    return LOGIN % "<div class=e>Wrong password.</div>", 401

@app.route("/logout")
def logout(): session.clear(); return redirect("/login")

@app.route("/healthz")
def healthz(): return "ok"

# ---- data ---------------------------------------------------------------
BLR = "(lower(j.location) LIKE '%bengaluru%' OR lower(j.location) LIKE '%bangalore%')"
IND = "(j.region IS NULL OR j.region!='UAE')"

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
    r = (s or "").split("\n", 1)[-1]; r = re.sub(r"\[LOCATION TIER \d: [\w ]+\]\s*", "", r); return r.strip()[:400]

def card(r):
    reg = "uae" if r["region"] == "UAE" else "blr"
    direct = r["application_url"] if r["application_url"] and "linkedin.com/signup" not in r["application_url"] else ""
    return {"id": hashlib.md5(r["url"].encode()).hexdigest()[:12], "url": r["url"], "title": r["title"], "company": company(r),
            "location": re.sub(r",\s*United Arab Emirates", "", r["location"] or ""), "source": (r["site"] or "").title(), "region": reg,
            "s1": r["fit_score"], "s2": r["check_score"], "years": r["years_min"] or 0, "why": clean(r["score_reasoning"]),
            "applyUrl": direct, "foundAt": r["discovered_at"],
            "decision": r["decision"] or "", "prev": r["prev_decision"] or "", "note": r["note"] or "", "skipReason": r["skip_reason"] or "",
            "companyUrl": r["company_apply_url"] or "", "resumeName": r["resume_name"] or "", "resumeMade": r["resume_made"] or "",
            "changes": json.loads(r["changes"] or "[]"), "reach": r["reach"] or "",
            "contact": json.loads(r["contact"] or "{}"), "outreach": json.loads(r["outreach"] or "{}"),
            "work": r["work_status"] or "", "workMsg": r["work_msg"] or "", "draft": r["draft_status"] or "", "draftMsg": r["draft_msg"] or ""}

@app.route("/api/jobs")
def jobs():
    q = f"""SELECT j.*, u.decision, u.prev_decision, u.note, u.skip_reason, u.company_apply_url, u.resume_name, u.resume_made, u.changes, u.reach, u.contact, u.outreach, u.work_status, u.work_msg, u.draft_status, u.draft_msg
            FROM jobs j LEFT JOIN ui_state u ON u.url=j.url
            WHERE ((j.fit_score>=7 AND j.check_score>=7 AND (j.years_min IS NULL OR j.years_min<5) AND
                   (j.region='UAE' OR ({IND} AND {BLR}))) OR (u.url IS NOT NULL AND u.decision!=''))
            ORDER BY (j.fit_score+IFNULL(j.check_score,0)) DESC, j.discovered_at DESC"""
    with db() as c: rows = [card(r) for r in c.execute(q)]
    return jsonify(rows)

FIELDS = {"decision": "decision", "note": "note", "skipReason": "skip_reason", "companyUrl": "company_apply_url", "reach": "reach", "prev": "prev_decision"}
DECISIONS = {"", "Make resume", "Ready to apply", "Applied manually", "Skip"}

@app.route("/api/job", methods=["POST"])
def update():
    d = request.get_json(force=True); url = d.get("url", "")
    patch = {FIELDS[k]: str(v)[:2000] for k, v in d.get("patch", {}).items() if k in FIELDS}
    if "decision" in patch and patch["decision"] not in DECISIONS: abort(400)
    if patch.get("company_apply_url") and not re.match(r"https?://", patch["company_apply_url"]): return jsonify(error="Link must start with http"), 400
    if not patch: abort(400)
    with db() as c:
        if not c.execute("SELECT 1 FROM jobs WHERE url=?", (url,)).fetchone(): abort(404)
        c.execute("INSERT OR IGNORE INTO ui_state(url) VALUES(?)", (url,))
        sets = ", ".join(f"{k}=?" for k in patch)
        c.execute(f"UPDATE ui_state SET {sets}, updated_at=datetime('now') WHERE url=?", (*patch.values(), url))
        if patch.get("decision") == "Make resume":
            c.execute("UPDATE ui_state SET work_status='queued', work_msg='' WHERE url=?", (url,))
        elif "decision" in patch:
            c.execute("UPDATE ui_state SET work_status='', work_msg='' WHERE url=?", (url,))
    if patch.get("decision") == "Make resume": enqueue("resume", url)
    return jsonify(ok=True)

@app.route("/api/retry", methods=["POST"])
def retry():
    url = request.get_json(force=True).get("url", "")
    with db() as c:
        if not c.execute("SELECT 1 FROM ui_state WHERE url=? AND decision='Make resume'", (url,)).fetchone(): abort(404)
        c.execute("UPDATE ui_state SET work_status='queued', work_msg='' WHERE url=?", (url,))
    enqueue("resume", url); return jsonify(ok=True)

@app.route("/api/contact", methods=["POST"])
def contact():
    d = request.get_json(force=True); url = d.get("url", ""); ct = {k: str(d.get("contact", {}).get(k, "")).strip()[:200] for k in ("name", "position", "email")}
    with db() as c:
        c.execute("INSERT OR IGNORE INTO ui_state(url) VALUES(?)", (url,))
        c.execute("UPDATE ui_state SET contact=?, updated_at=datetime('now') WHERE url=?", (json.dumps(ct), url))
    return jsonify(ok=True)

@app.route("/api/draft", methods=["POST"])
def draft():
    url = request.get_json(force=True).get("url", "")
    with db() as c:
        r = c.execute("SELECT contact FROM ui_state WHERE url=?", (url,)).fetchone()
        ct = json.loads(r["contact"] or "{}") if r else {}
        if not (ct.get("name") and ct.get("position") and re.fullmatch(r"[^ @]+@[^ @]+\.[^ @]+", ct.get("email", ""))):
            return jsonify(error="Fill in name, position and a valid email first."), 400
        c.execute("UPDATE ui_state SET draft_status='queued', draft_msg='' WHERE url=?", (url,))
    enqueue("draft", url); return jsonify(ok=True)

@app.route("/api/outreach", methods=["POST"])
def outreach():
    d = request.get_json(force=True); url, key, val = d.get("url", ""), d.get("key", ""), d.get("val", "")
    if key not in ("sentAt", "repliedAt"): abort(400)
    with db() as c:
        r = c.execute("SELECT outreach FROM ui_state WHERE url=?", (url,)).fetchone()
        if not r: abort(404)
        o = json.loads(r["outreach"] or "{}")
        if val == "now": o[key] = int(time.time() * 1000)
        else:
            o.pop(key, None)
            if key == "sentAt": o.pop("repliedAt", None)
        c.execute("UPDATE ui_state SET outreach=? WHERE url=?", (json.dumps(o), url))
    return jsonify(ok=True)

@app.route("/api/stats")
def stats():
    try: return jsonify(json.loads(STATE.read_text()))
    except Exception: return jsonify({})

@app.route("/resume/<name>")
def resume(name):
    if not re.fullmatch(r"[\w.\- ]+\.pdf", name): abort(404)
    return send_from_directory(RESUMES, name, as_attachment=True)

# ---- background worker: one task at a time, started by actions on the site -----
import queue, subprocess, threading
LIBPY = "/home/rohan/claude-chats/apply-pilot/apply-pilot-Updated-main/.venv/bin/python"
TASKS = queue.Queue(); PENDING = set()
SCRIPT = {"resume": ("make_resumes.py", "work_status", "work_msg", 1500), "draft": ("draft_email.py", "draft_status", "draft_msg", 300)}

def enqueue(kind, url):
    if (kind, url) in PENDING: return
    PENDING.add((kind, url)); TASKS.put((kind, url))

def worker():
    while True:
        kind, url = TASKS.get()
        script, scol, mcol, tmo = SCRIPT[kind]
        try:
            with db() as c:
                c.execute(f"UPDATE ui_state SET {scol}='working', {mcol}='' WHERE url=?", (url,))
            p = subprocess.run([LIBPY, str(BASE / "webapp" / script), "--url", url], capture_output=True, text=True, timeout=tmo, cwd=str(BASE))
            last = (p.stdout.strip().splitlines() or [""])[-1][:300]
            ok = p.returncode == 0
            with db() as c:
                c.execute(f"UPDATE ui_state SET {scol}=?, {mcol}=? WHERE url=?", ("" if ok else "failed", "" if ok else (last or (p.stderr.strip().splitlines() or ["Failed"])[-1][:300]), url))
        except subprocess.TimeoutExpired:
            with db() as c: c.execute(f"UPDATE ui_state SET {scol}='failed', {mcol}='Timed out. Tap Retry.' WHERE url=?", (url,))
        except Exception as e:
            with db() as c: c.execute(f"UPDATE ui_state SET {scol}='failed', {mcol}=? WHERE url=?", (repr(e)[:200], url))
        finally:
            PENDING.discard((kind, url)); TASKS.task_done()

def resume_unfinished():
    """On start: anything left queued/working, or sitting in Make resume without a result, goes back on the queue."""
    with db() as c:
        for r in c.execute("SELECT url FROM ui_state WHERE decision='Make resume' AND work_status!='failed'"):
            c.execute("UPDATE ui_state SET work_status='queued' WHERE url=?", (r["url"],)); enqueue("resume", r["url"])
        for r in c.execute("SELECT url FROM ui_state WHERE draft_status IN ('queued','working')"):
            enqueue("draft", r["url"])

for _ in range(3): threading.Thread(target=worker, daemon=True).start()   # 3 jobs at a time
resume_unfinished()

@app.route("/")
def index(): return Response((Path(__file__).parent / "index.html").read_text(), mimetype="text/html")

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="127.0.0.1", port=8787, threads=4)
