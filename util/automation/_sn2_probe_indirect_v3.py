"""Simpler indirect-chunk probe — walks the SDFile chunk stream linearly."""

import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT = r"E:\tmp_dir\probe_v3.log"

def log(msg):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)

# Truncate the log
with open(OUT, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore

log("open_capture...")
CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
cap, controller = _lib.open_capture(CAPTURE)
try:
    log("get sdfile...")
    sdfile = controller.GetStructuredFile()
    try:
        n = sdfile.chunks.size()
    except Exception:
        n = len(sdfile.chunks)
    log(f"got sdfile with {n} chunks")

    # Linear scan: find every ExecuteIndirect chunk and print its arg buffer
    log("scanning for ExecuteIndirect chunks...")
    found = 0
    out_rows = []
    for i in range(n):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "ExecuteIndirect" not in cname:
            continue
        try:
            eid = int(chunk.metadata.eventId)
        except Exception:
            eid = None
        arg_buf = None
        arg_off = None
        for j in range(chunk.NumChildren()):
            c = chunk.GetChild(j)
            cn = str(c.name)
            try:
                if cn == "pArgumentBuffer":
                    arg_buf = str(c.AsResourceId())
                elif cn == "ArgumentBufferOffset":
                    arg_off = int(c.AsInt())
            except Exception:
                pass
        out_rows.append((eid, arg_buf, arg_off, cname))
        found += 1
    log(f"found {found} ExecuteIndirect chunks")
    log("first 20:")
    for eid, ab, ao, cn in out_rows[:20]:
        log(f"  eid={eid} arg={ab} +{ao}  ({cn})")
    log(f"... and {max(0, found - 20)} more")

    # Save the full list
    import json
    with open(r"E:\tmp_dir\sn2_invest_v5\execute_indirect_chunks.json", "w") as f:
        json.dump([
            {"eventId": e, "argBuffer": a, "argOffset": o, "chunkName": cn}
            for e, a, o, cn in out_rows
        ], f, indent=2, default=str)
    log("wrote execute_indirect_chunks.json")
finally:
    controller.Shutdown()
    cap.Shutdown()
log("DONE")
os._exit(0)
