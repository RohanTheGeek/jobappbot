"""Usage: make_sync_files.py <existing_jobs_dir> <out_dir>
Reads sync_batch.json, skips ids already present as <existing_jobs_dir>/<id>.json, writes one JSON file per
new job into <out_dir>, and prints a compact `writes` array for ArtifactData batch (max 50 per call)."""
import json, os, sys
ex = {f[:-5] for f in os.listdir(sys.argv[1])} if os.path.isdir(sys.argv[1]) else set()
out = sys.argv[2]; os.makedirs(out, exist_ok=True)
batch = json.load(open("/home/rohan/.applypilot/scan/sync_batch.json"))
new = sorted((d for d in batch if d["id"] not in ex), key=lambda d: -(d["s1"] + d["s2"]))
w = []
for i, d in enumerate(new):
    d["rank"] = -1000 + i
    p = os.path.join(out, d["id"] + ".json"); json.dump(d, open(p, "w"))
    w.append({"op": "set", "collection": "jobs", "doc_id": d["id"], "file_path": p})
print(f"# {len(batch)} in batch, {len(new)} new, {len(batch)-len(new)} already on dashboard; chunks of 50:")
for i in range(0, len(w), 50): print(json.dumps(w[i:i+50], separators=(",", ":")))
