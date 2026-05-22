"""For the SLW composite L/R pair (events 20876 / 20954), enumerate
every root CBV on both eyes and identify the per-eye View cbuffer.

Strategy:
  1. Both 20876 and 20954 actually run (SLW composite executes on both eyes
     unlike the dead water basepass).
  2. They share the root signature; the View cbuffer is at the same root
     parameter slot.
  3. For each root CBV on both events, compare (resource, offset, size). If
     resource matches but offset differs → that's a per-eye cbuffer.
  4. The "View" cbuffer is identifiable by being PS b0 in standard UE5
     shaders.

Output: per-root-CBV mapping {left: {resource, offset, size}, right: {resource,
offset, size}, isPerEye: bool} plus a focused "viewCBuffer" summary that
UEVR can use to inject right-eye View into the water basepass replay.

Bonus: do the same diff for the opaque-basepass L/R pair (L=11870/R=13157,
already confirmed real per-eye basepass) and compare — that's a sanity
check (both events should have the same per-eye View cbuffer layout).
"""

import hashlib
import json
import os
import struct
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_target"
JSON_OUT = os.path.join(OUT_DIR, "right_view_cbuffer.json")
LOG = os.path.join(OUT_DIR, "right_view_cbuffer.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# The two L/R pairs we know are real
PAIRS = [
    ("SLW_composite", 20876, 20954),     # SingleLayerWaterCompositePS — both eyes
    ("opaque_basepass", 11870, 13157),   # Opaque MainPS — both eyes
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def collect_root_cbvs(controller, eid):
    """Return list of (root_param_index, visibility, register, resource, offset, size, data_md5)."""
    controller.SetFrameEvent(int(eid), True)
    d3d12 = controller.GetD3D12PipelineState()
    try:
        params = d3d12.rootSignature.parameters
    except Exception:
        return []
    out = []
    for i, p in enumerate(params):
        try:
            d = p.descriptor
        except Exception:
            d = None
        if d is None:
            continue
        rid = res_id(d.resource) if hasattr(d, "resource") else None
        if not rid or rid == "ResourceId::0":
            continue
        dt = str(d.type).split(".")[-1] if hasattr(d, "type") else None
        if dt != "ConstantBuffer":
            continue
        offset = to_int(d.byteOffset) if hasattr(d, "byteOffset") else None
        size = to_int(d.byteSize) if hasattr(d, "byteSize") else None
        vis = str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None
        reg = to_int(p.reg) if hasattr(p, "reg") else None
        space = to_int(p.space) if hasattr(p, "space") else None
        # Pull the actual bytes for hashing (up to 512 bytes to keep it fast)
        head_md5 = None
        try:
            head = bytes(controller.GetBufferData(
                d.resource, int(offset or 0),
                min(int(size or 256), 512)))
            head_md5 = hashlib.md5(head).hexdigest()[:16]
        except Exception:
            pass
        out.append({
            "rootIndex": i, "visibility": vis,
            "register": reg, "space": space,
            "resource": rid, "byteOffset": offset, "byteSize": size,
            "headMd5": head_md5,
        })
    return out


def compare_pair(controller, label, L_eid, R_eid):
    log(f"\n=== {label}: L={L_eid} R={R_eid} ===")
    l_cbvs = collect_root_cbvs(controller, L_eid)
    r_cbvs = collect_root_cbvs(controller, R_eid)
    log(f"  L root CBVs: {len(l_cbvs)}")
    log(f"  R root CBVs: {len(r_cbvs)}")

    # Pair by (visibility, register, space)
    def key(c):
        return (c["visibility"], c["register"], c["space"])
    l_by_key = {key(c): c for c in l_cbvs}
    r_by_key = {key(c): c for c in r_cbvs}
    keys = sorted(set(l_by_key.keys()) | set(r_by_key.keys()))

    rows = []
    per_eye_count = 0
    shared_count = 0
    for k in keys:
        lc = l_by_key.get(k); rc = r_by_key.get(k)
        row = {"visibility": k[0], "register": k[1], "space": k[2]}
        if lc:
            row["L"] = {"resource": lc["resource"], "offset": lc["byteOffset"],
                        "size": lc["byteSize"], "headMd5": lc["headMd5"]}
        if rc:
            row["R"] = {"resource": rc["resource"], "offset": rc["byteOffset"],
                        "size": rc["byteSize"], "headMd5": rc["headMd5"]}
        if lc and rc:
            if lc["resource"] == rc["resource"] and lc["byteOffset"] == rc["byteOffset"]:
                row["mode"] = "shared"
                shared_count += 1
            elif lc["resource"] == rc["resource"] and lc["byteOffset"] != rc["byteOffset"]:
                row["mode"] = "per_eye_same_pool"
                per_eye_count += 1
                row["offsetDelta"] = rc["byteOffset"] - lc["byteOffset"]
            elif lc["resource"] != rc["resource"]:
                row["mode"] = "per_eye_different_resource"
                per_eye_count += 1
            else:
                row["mode"] = "mixed"
        rows.append(row)

    log(f"  shared CBVs:   {shared_count}")
    log(f"  per-eye CBVs:  {per_eye_count}")
    for row in rows:
        if row.get("mode") not in ("shared",):
            l_str = f"{row.get('L', {}).get('resource')} +{row.get('L', {}).get('offset')}" if row.get("L") else "—"
            r_str = f"{row.get('R', {}).get('resource')} +{row.get('R', {}).get('offset')}" if row.get("R") else "—"
            log(f"    {row['visibility']} b{row['register']} (space {row['space']}): "
                f"L={l_str}  R={r_str}  mode={row.get('mode')}  delta={row.get('offsetDelta')}")
    return {"label": label, "L_event": L_eid, "R_event": R_eid,
            "L_cbvs": l_cbvs, "R_cbvs": r_cbvs, "rows": rows,
            "perEyeCount": per_eye_count, "sharedCount": shared_count}


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    results = []
    for label, L, R in PAIRS:
        r = compare_pair(controller, label, L, R)
        results.append(r)

    out = {"results": results}
    with open(JSON_OUT, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nwrote {JSON_OUT}")

    # Focused summary: which root CBV is the View cbuffer (PS b0)?
    log("\n=== SUMMARY: per-eye View cbuffer offset (PS b0) ===")
    for r in results:
        ps_b0 = [row for row in r["rows"]
                 if row.get("visibility") == "Pixel" and row.get("register") == 0]
        for row in ps_b0:
            L = row.get("L"); R = row.get("R")
            if L and R:
                log(f"  {r['label']}:")
                log(f"    Left  View cbuffer: {L['resource']} +{L['offset']} size {L['size']}")
                log(f"    Right View cbuffer: {R['resource']} +{R['offset']} size {R['size']}")
                log(f"    Mode: {row.get('mode')}  Offset delta: {row.get('offsetDelta')}")

    log("\n=== UEVR INJECTION HINT ===")
    log("To replay the 14 water basepass draws (events 20618-20793) for the right eye:")
    log("  1. For each draw, swap PS root param [4] (View cbuffer) and any other per-eye CBV")
    log("     from left-eye offsets to the right-eye offsets surfaced above.")
    log("  2. Change viewport from (0,0,427,481) to right-eye viewport (see SLW composite at R=20954)")
    log("  3. Re-execute via ExecuteCommandLists with the patched root sig bindings.")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
