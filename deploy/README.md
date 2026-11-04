# Deployment files

Runtime scripts that run on the Raspberry Pi next to the ApplyPilot library.

- `scan/scan.py`: the 4-hourly job scan (cron). Needs `~/.applypilot/searches.yaml` and `searches_uae.yaml`.
- `webapp/`: the password-protected dashboard (Flask + waitress) on port 8787. It reads `applypilot.db` directly and starts resume and email drafting in the background when you act on a job. Create `webapp/webapp.env` with `APP_PASSWORD=...` before starting.

Private data (API keys, proxy login, resume, profile, database) is not in this repository and must never be committed.
