"""Stage B only — descriptor table per-eye check on the SLW composite pair
in capture A. Small, fast, focused.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_uevr_followup"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "B_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def collect_all_root_state(controller, eid):
    controller.SetFrameEvent(int(eid), True)
    d3d12 = controller.GetD3D12PipelineState()
    try:
        params = d3d12.rootSignature.parameters
    except Exception:
        return []
    out = []
    for i, p in enumerate(params):
        entry = {
            "index": i,
            "visibility": str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None,
            "register": to_int(p.reg) if hasattr(p, "reg") else None,
            "space": to_int(p.space) if hasattr(p, "space") else None,
        }
        try:
            d = p.descriptor
            if d is not None and getattr(d, "resource", None) is not None:
                rid = res_id(d.resource)
                if rid and rid != "ResourceId::0":
                    entry["descriptor"] = {
                        "resource": rid,
                        "byteOffset": to_int(d.byteOffset),
                        "byteSize": to_int(d.byteSize),
                        "type": str(d.type).split(".")[-1] if hasattr(d, "type") else None,
                    }
        except Exception:
            pass
        try:
            heap = res_id(p.heap)
            if heap and heap != "ResourceId::0":
                entry["table"] = {
                    "heap": heap,
                    "heapByteOffset": to_int(p.heapByteOffset),
                }
        except Exception:
            pass
        out.append(entry)
    return out


cap, controller = _lib.open_capture(CAPTURE)
try:
    # Use the SLW composite pair
    L_slw, R_slw = 20876, 20954
    # Also include the opaque basepass pair (different root sig)
    log("=== B. DESCRIPTOR TABLE per-eye (SLW composite + opaque basepass) ===")
    pairs = [("SLW_composite", L_slw, R_slw),
             ("opaque_basepass", 11870, 13157)]
    all_results = {}
    for label, L, R in pairs:
        log(f"\n  --- {label}: L={L} R={R} ---")
        l_root = collect_all_root_state(controller, L)
        r_root = collect_all_root_state(controller, R)
        table_rows = []
        for lp, rp in zip(l_root, r_root):
            if "table" not in lp or "table" not in rp:
                continue
            lt = lp["table"]; rt = rp["table"]
            mode = "shared" if (lt["heap"] == rt["heap"] and
                                lt["heapByteOffset"] == rt["heapByteOffset"]) else "per_eye"
            table_rows.append({
                "rootIndex": lp["index"],
                "visibility": lp["visibility"],
                "register": lp["register"],
                "L_heap": lt["heap"], "L_offset": lt["heapByteOffset"],
                "R_heap": rt["heap"], "R_offset": rt["heapByteOffset"],
                "mode": mode,
                "offsetDelta": rt["heapByteOffset"] - lt["heapByteOffset"] if mode == "per_eye" else 0,
            })
        log(f"  tables found: {len(table_rows)}")
        for t in table_rows:
            log(f"    [{t['rootIndex']}] {t['visibility']} reg{t['register']}: "
                f"L=heap{t['L_heap']}+{t['L_offset']} R=heap{t['R_heap']}+{t['R_offset']}  "
                f"mode={t['mode']}  Δ={t['offsetDelta']}")
        per_eye = sum(1 for t in table_rows if t['mode'] == 'per_eye')
        log(f"  per-eye: {per_eye}/{len(table_rows)}")
        all_results[label] = table_rows
    with open(os.path.join(OUT_DIR, "B_descriptor_tables.json"), "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    log("wrote B_descriptor_tables.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
