"""Quick query: for every pair in v4/a_event_pairs.json, look at the chunk
metadata for the right-eye event and check if it's a Dispatch with X=0.

Doesn't need eye-classifier or per-event SetFrameEvent — just walks the
SDFile chunk stream once. Fast.
"""

import json
import os
import sys

OUT = os.environ.get("SN2_QUERY_OUT", r"E:\tmp_dir\sn2_invest_v5\dead_dispatches.json")
V4_DIR = os.environ.get("SN2_V4_DIR", r"E:\tmp_dir\sn2_invest_v4")
CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)

sys.path.insert(0, r"E:\Github\renderdoc")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402


with open(os.path.join(V4_DIR, "a_event_pairs.json")) as f:
    pairs_data = json.load(f)
pairs = pairs_data["pairs"]
print(f"loaded {len(pairs)} pairs from v4")

cap, controller = _lib.open_capture(CAPTURE)
try:
    # Build action_by_eid (no SetFrameEvent — uses cached structured file)
    action_by_eid = {}
    for a in _lib.walk_actions(controller):
        action_by_eid[int(a.eventId)] = a

    print(f"indexed {len(action_by_eid)} actions")

    rows = []
    dead = []
    for p in pairs:
        L = int(p["left"])
        R = int(p["right"])
        la = action_by_eid.get(L)
        ra = action_by_eid.get(R)
        if la is None or ra is None:
            continue
        try:
            ld = [int(v) for v in la.dispatchDimension]
        except Exception:
            ld = None
        try:
            rd_ = [int(v) for v in ra.dispatchDimension]
        except Exception:
            rd_ = None
        if not ld or not rd_:
            continue
        if all(v == 0 for v in ld) and all(v == 0 for v in rd_):
            continue
        if ld == rd_:
            continue
        row = {
            "leftEvent": L,
            "rightEvent": R,
            "leftName": str(la.GetName(controller.GetStructuredFile())),
            "rightName": str(ra.GetName(controller.GetStructuredFile())),
            "leftDispatch": ld,
            "rightDispatch": rd_,
            "rightDead": all(v == 0 for v in rd_),
            "leftDead": all(v == 0 for v in ld),
        }
        rows.append(row)
        if row["rightDead"] and not row["leftDead"]:
            dead.append(row)

    out = {
        "summary": {
            "pairsChecked": len(pairs),
            "pairsWithDispatchData": len(rows),
            "deadRightOnly": len(dead),
            "deadLeftOnly": sum(1 for r in rows if r["leftDead"] and not r["rightDead"]),
        },
        "deadRight": dead,
        "allDispatchDeltas": rows,
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    print(f"wrote {OUT}")
    print(f"summary: {out['summary']}")
    print()
    print("Dead right-eye dispatches (first 10):")
    for r in dead[:10]:
        print(f"  L={r['leftEvent']} ({r['leftName'][:60]})")
        print(f"    L dim={r['leftDispatch']}")
        print(f"    R={r['rightEvent']} ({r['rightName'][:60]})")
        print(f"    R dim={r['rightDispatch']}")
        print()
finally:
    controller.Shutdown()
    cap.Shutdown()
