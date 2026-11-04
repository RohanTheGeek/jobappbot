"""draft_email.py --url URL : write a cold outreach email for a job's contact into ui_state.outreach. Nothing is ever sent."""
import json, re, sqlite3, sys, time
sys.path.insert(0, "/home/rohan/claude-chats/apply-pilot/apply-pilot-Updated-main")
from applypilot.config import RESUME_PATH, load_env, load_profile
load_env()
from applypilot.llm import get_client
url = sys.argv[sys.argv.index("--url") + 1]
c = sqlite3.connect("/home/rohan/.applypilot/applypilot.db", timeout=60); c.row_factory = sqlite3.Row
j = c.execute("SELECT j.*, u.contact FROM jobs j JOIN ui_state u ON u.url=j.url WHERE j.url=?", (url,)).fetchone()
ct = json.loads(j["contact"] or "{}"); p = load_profile().get("personal", {})
dubai = (j["region"] or "") == "UAE"
cand = f"{p.get('full_name','')} | {p.get('phone','')} | {p.get('email','')} | {p.get('linkedin_url','')}"
METRICS = ("500+ client engagements, 96-98% batch success, ~4,500 candidates screened, 80%+ run-time reduction, ~10x run-cost reduction, 2,500+ respondents automated, "
           "~25% screen-in/conversion lift, 200+ client consultations, ~$400K revenue, ~30% MoM account revenue growth, 80-90%+ efficiency gains in demos, 600+ cases/day, "
           "15+ production issues resolved, 1M+ events/day, 30% ransomware detection accuracy gain, 40% triage time reduction, 25% ticket resolution reduction")
rules = ("You write short, honest cold outreach emails from a job seeker to a hiring manager or recruiter.\n\nRules:\n"
 "- Use ONLY facts from the resume and metrics below. Never invent employers, numbers, tools or claims.\n"
 "- Plain, warm, confident, human tone. No hype, no buzzwords, no em dashes.\n- 140-190 words in the body. Short paragraphs. Up to 3 short bullets allowed for proof points.\n"
 "- Structure: one-line hook naming the role and company, then 2-3 proof points from the resume that match what the role needs, then "
 + ("one sentence saying he is based in Bangalore, India, and is willing to relocate to Dubai, or work remotely if the company allows it" if dubai else "one sentence saying he is based in Bengaluru and can join the team in person")
 + ", then a soft call to action (a short call; resume attached).\n- Address the contact by first name. Sign off with the candidate name, phone, email and LinkedIn.\n"
 "- Output exactly: a first line starting with Subject: then a blank line then the email body. Nothing else.")
user = (f"CANDIDATE: {cand}\n\nRESUME:\n{RESUME_PATH.read_text(encoding='utf-8')}\n\nVERIFIED METRICS: {METRICS}\n\n"
        f"JOB: {j['title']} ({j['location']})\nJOB DESCRIPTION:\n{(j['full_description'] or '')[:3000]}\n\nCONTACT: {ct.get('name')}, {ct.get('position')}, {ct.get('email')}")
t = get_client().chat([{"role": "system", "content": rules}, {"role": "user", "content": user}], max_tokens=1200, temperature=0.5).replace("—", ",").strip()
m = re.match(r"Subject:\s*(.*?)\n+([\s\S]*)$", t, re.I)
out = {"subject": m.group(1).strip() if m else f"Reaching out about {j['title']}", "body": (m.group(2) if m else t).strip(), "at": int(time.time() * 1000)}
old = json.loads(c.execute("SELECT outreach FROM ui_state WHERE url=?", (url,)).fetchone()[0] or "{}")
out.update({k: old[k] for k in ("sentAt", "repliedAt") if k in old and False})   # a redraft resets tracking
c.execute("UPDATE ui_state SET outreach=?, draft_status='', draft_msg='' WHERE url=?", (json.dumps(out), url)); c.commit()
print("OK drafted")
