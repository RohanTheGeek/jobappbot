"""One-off: import decisions/notes/resume info from the old Claude artifact (files pulled with ArtifactData) into ui_state."""
import json, os, sqlite3, sys
src = sys.argv[1]
c = sqlite3.connect("/home/rohan/.applypilot/applypilot.db", timeout=30)
c.execute("""CREATE TABLE IF NOT EXISTS ui_state(url TEXT PRIMARY KEY, decision TEXT DEFAULT '', prev_decision TEXT DEFAULT '', note TEXT DEFAULT '',
  skip_reason TEXT DEFAULT '', company_apply_url TEXT DEFAULT '', resume_name TEXT DEFAULT '', resume_made TEXT DEFAULT '', changes TEXT DEFAULT '[]',
  reach TEXT DEFAULT '', contact TEXT DEFAULT '{}', outreach TEXT DEFAULT '{}', updated_at TEXT)""")
n = miss = 0
for f in os.listdir(src):
    d = json.load(open(os.path.join(src, f))); d = d.get("data", d)
    if not (d.get("decision") or d.get("note") or d.get("resumeName") or d.get("reach") or d.get("outreach") or d.get("companyApplyUrl")): continue
    if not c.execute("SELECT 1 FROM jobs WHERE url=?", (d["jobUrl"],)).fetchone(): miss += 1; continue
    c.execute("""INSERT INTO ui_state(url,decision,prev_decision,note,skip_reason,company_apply_url,resume_name,resume_made,changes,reach,contact,outreach,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(url) DO NOTHING""",
      (d["jobUrl"], d.get("decision",""), d.get("prevDecision",""), d.get("note",""), d.get("skipReason",""), d.get("companyApplyUrl",""),
       d.get("resumeName",""), d.get("resumeMade",""), json.dumps(d.get("changes",[])), d.get("reach",""), json.dumps(d.get("contact",{})), json.dumps(d.get("outreach",{}))))
    n += 1
c.commit(); print("imported", n, "missing in db", miss)
print(c.execute("select decision,count(*) from ui_state group by 1").fetchall())
