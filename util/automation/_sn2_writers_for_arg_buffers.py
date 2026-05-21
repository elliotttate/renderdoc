"""Run compute_writers.find_writers on the top-N indirect arg buffers."""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_LOG = r"E:\tmp_dir\writers_log.log"
OUT_JSON = r"E:\tmp_dir\sn2_invest_v5\arg_buf_writers.json"

def log(msg):
    with open(OUT_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)

with open(OUT_LOG, "w"): pass

from util.automation import _lib, compute_writers  # type: ignore

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# Top arg buffers from the indirect-chunk analysis
TARGETS = [
    "ResourceId::27785",   # 251 uses
    "ResourceId::27330",   # 246 uses
    "ResourceId::29787",   # 167 uses
    "ResourceId::44164",   # 59 uses
    "ResourceId::27768",   # 56 uses (Lumen tile build was here)
    "ResourceId::27789",   # 56 uses
]

results = {}
for target in TARGETS:
    log(f"finding writers for {target}...")
    t0 = time.time()
    try:
        w = compute_writers.find_writers(CAPTURE, target, eye_classify=False)
        results[target] = {
            "summary": w.get("summary", {}),
            "writes": w.get("writes", []),
        }
        s = w.get("summary", {})
        log(f"  {target}: {s.get('writeCount', 0)} writers, "
            f"{s.get('deadDispatches', 0)} dead in {time.time()-t0:.1f}s")
    except Exception as e:
        log(f"  {target}: ERROR {e}")
        results[target] = {"error": str(e)}

with open(OUT_JSON, "w") as f:
    json.dump(results, f, indent=2, default=str)
log(f"wrote {OUT_JSON}")
log("DONE")
os._exit(0)
