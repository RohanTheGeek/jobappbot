import sys, sqlite3, httpx, time, json
sys.path.insert(0, "/home/rohan/claude-chats/apply-pilot/apply-pilot-Updated-main")
from applypilot.config import RESUME_PATH, load_env, load_profile
load_env()
import os
from applypilot.scoring.scorer import score_job
from applypilot.scoring.tailor import tailor_resume
key = os.environ["KIE_API_KEY"]
def credit(): return httpx.get("https://api.kie.ai/api/v1/chat/credit", headers={"Authorization": f"Bearer {key}"}, timeout=30).json()["data"]
c = sqlite3.connect("file:/home/rohan/.applypilot/applypilot.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
jobs = [dict(r) for r in c.execute("SELECT * FROM jobs WHERE length(full_description)>1500 AND fit_score>=7 ORDER BY RANDOM() LIMIT 12")]
resume, profile = RESUME_PATH.read_text(), load_profile()
b0 = credit(); t = time.time()
for j in jobs[:10]: score_job(resume, j)
b1 = credit(); print(f"scoring: 10 calls, {b0-b1:.2f} credits => {(b0-b1)/10:.3f} per call ({time.time()-t:.0f}s)", flush=True)
t = time.time(); atts = 0
for j in jobs[10:12]:
    _, rep = tailor_resume(resume, j, profile); atts += rep["attempts"]
b2 = credit(); print(f"tailor: 2 resumes ({atts} attempts), {b1-b2:.2f} credits => {(b1-b2)/2:.2f} per resume ({time.time()-t:.0f}s)", flush=True)
print("balance now", b2)
