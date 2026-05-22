"""UEVR-followup investigation — three questions in one pass:

A. CBUFFER STABILITY: is the per-eye View offset within ResourceId::29343
   stable, or does UE5's UB allocator pick a different offset each frame?
   Re-run cbuffer finder on a second SN2 capture and diff.

B. DESCRIPTOR TABLE PER-EYE: are the 4 descriptor tables in the water
   root sig per-eye (different heap offsets between L/R) or shared? If
   per-eye, UEVR has more swaps to do beyond just the View cbuffer.

C. OTHER SHARED-UAV CONSUMERS: for HZB (29816), Lumen reflection tiles
   (27768), and UWE fog (30092/30095), walk the basepass draws on each
   eye and check whether right-eye basepass binds them. Same MRT/SRV
   pattern as the SLW investigation. Output: "for each subsystem, is
   the bug at MRT-level / mesh-batch-level / classifier miscount?"
"""

import hashlib
import json
import os
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_uevr_followup"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

# Capture A is the one we've been analysing all session
CAPTURE_A = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
# Capture B is a different no-UEVR frame for stability comparison
CAPTURE_B = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame2012.rdc"

# Known shared-UAV resources from the May 16 / our earlier work
OTHER_SHARED_UAVS = [
    "ResourceId::29816",  # HZB
    "ResourceId::27768",  # Lumen reflection tiles
    "ResourceId::30092",  # UWE fog denoise out
    "ResourceId::30095",  # UWE fog denoise out 2
    "ResourceId::30086",  # VSM (already patched by Sn2VsmUbClampPatch)
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def collect_all_root_state(controller, eid):
    """Dump every root param (CBV + descriptor table) at an event."""
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
        # Root CBV/SRV/UAV
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
        # Descriptor table
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


def find_pair_for_capture(controller, capture_path):
    """Find L/R basepass pair using the same approach as drill_v3."""
    log(f"  walking actions for basepass candidates...")
    main_rt = None
    # Find most-common-shape RT
    shape_counts = {}
    textures_by_id = {str(t.resourceId): t for t in controller.GetTextures()}
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        outs = list(a.outputs) if a.outputs else []
        if not outs:
            continue
        rid = res_id(outs[0])
        tex = textures_by_id.get(rid)
        if tex is None:
            continue
        w, h = int(tex.width), int(tex.height)
        if w == h and w >= 1024:
            continue
        shape_counts[(w, h)] = shape_counts.get((w, h), 0) + 1
    if not shape_counts:
        return None, None
    main_shape = max(shape_counts.items(), key=lambda kv: kv[1])[0]
    log(f"    main RT shape: {main_shape}")

    # Walk MainPS draws targeting an RT of that shape
    main_ps_draws = []
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        outs = list(a.outputs) if a.outputs else []
        if not outs:
            continue
        rid = res_id(outs[0])
        tex = textures_by_id.get(rid)
        if tex is None:
            continue
        if (int(tex.width), int(tex.height)) != main_shape:
            continue
        eid = int(a.eventId)
        controller.SetFrameEvent(eid, True)
        pipe = controller.GetPipelineState()
        try:
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        except Exception:
            refl = None
        if refl is None or len(refl.rawBytes) == 0:
            continue
        entry_pt = str(refl.entryPoint)
        if entry_pt != "MainPS":
            continue
        h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
        main_ps_draws.append({"eventId": eid, "hash": h, "numIndices": int(a.numIndices)})
    if len(main_ps_draws) < 4:
        log(f"    only {len(main_ps_draws)} MainPS draws — can't pair")
        return None, None
    # Group by hash, find one with both halves
    from collections import defaultdict
    by_hash = defaultdict(list)
    for d in main_ps_draws:
        by_hash[d["hash"]].append(d)
    best = max(by_hash.keys(),
               key=lambda h: (max(d["eventId"] for d in by_hash[h])
                              - min(d["eventId"] for d in by_hash[h])) * len(by_hash[h]))
    lst = sorted(by_hash[best], key=lambda d: d["eventId"])
    eids = [d["eventId"] for d in lst]
    mid = (eids[0] + eids[-1]) // 2
    early = [d for d in lst if d["eventId"] <= mid]
    late = [d for d in lst if d["eventId"] > mid]
    if not early or not late:
        return None, None
    L = max(early, key=lambda d: d["numIndices"])["eventId"]
    R = max(late, key=lambda d: d["numIndices"])["eventId"]
    log(f"    picked basepass pair: hash={best[:16]} L={L} R={R}")
    return int(L), int(R)


# =====================================================================
# A. CBUFFER STABILITY across captures
# =====================================================================
log("=== A. CBUFFER STABILITY (cross-capture comparison) ===")
cbuffer_results = []
for cap_path in (CAPTURE_A, CAPTURE_B):
    if not os.path.isfile(cap_path):
        log(f"  capture missing: {cap_path}")
        continue
    log(f"\n--- capture: {os.path.basename(cap_path)} ---")
    cap, controller = _lib.open_capture(cap_path)
    try:
        L, R = find_pair_for_capture(controller, cap_path)
        if L is None:
            log(f"  no pair found")
            continue
        log(f"  picked L={L} R={R}")
        l_root = collect_all_root_state(controller, L)
        r_root = collect_all_root_state(controller, R)
        # Find Pixel b0
        ps_b0_l = next((p for p in l_root
                        if p.get("visibility") == "Pixel" and p.get("register") == 0
                        and "descriptor" in p), None)
        ps_b0_r = next((p for p in r_root
                        if p.get("visibility") == "Pixel" and p.get("register") == 0
                        and "descriptor" in p), None)
        entry = {
            "capture": cap_path,
            "L_event": L, "R_event": R,
            "ps_b0_L": ps_b0_l.get("descriptor") if ps_b0_l else None,
            "ps_b0_R": ps_b0_r.get("descriptor") if ps_b0_r else None,
        }
        cbuffer_results.append(entry)
        if ps_b0_l and ps_b0_r:
            log(f"  PS b0 L: {ps_b0_l['descriptor']['resource']} +{ps_b0_l['descriptor']['byteOffset']}")
            log(f"  PS b0 R: {ps_b0_r['descriptor']['resource']} +{ps_b0_r['descriptor']['byteOffset']}")
            log(f"  Δ: {ps_b0_r['descriptor']['byteOffset'] - ps_b0_l['descriptor']['byteOffset']}")
    finally:
        controller.Shutdown()
        cap.Shutdown()

# Cross-capture comparison
log("\n--- CROSS-CAPTURE COMPARISON ---")
if len(cbuffer_results) >= 2:
    a, b = cbuffer_results[0], cbuffer_results[1]
    log(f"  Capture A (frame ~610): L PS b0 offset = {a['ps_b0_L']['byteOffset']}  R = {a['ps_b0_R']['byteOffset']}")
    log(f"  Capture B (frame ~2012): L PS b0 offset = {b['ps_b0_L']['byteOffset']}  R = {b['ps_b0_R']['byteOffset']}")
    a_delta = a['ps_b0_R']['byteOffset'] - a['ps_b0_L']['byteOffset']
    b_delta = b['ps_b0_R']['byteOffset'] - b['ps_b0_L']['byteOffset']
    log(f"  Δ in A: {a_delta}  Δ in B: {b_delta}")
    if a_delta == b_delta and a['ps_b0_L']['byteOffset'] == b['ps_b0_L']['byteOffset']:
        log("  VERDICT: STABLE — offsets identical across frames. UEVR can hardcode.")
    elif a_delta == b_delta:
        log("  VERDICT: DELTA STABLE — absolute offsets shift per frame but the L↔R delta is")
        log("           constant. UEVR can compute right offset from observed left offset.")
    else:
        log("  VERDICT: UNSTABLE — UEVR must detect per-eye offsets at runtime (no hardcoding).")

with open(os.path.join(OUT_DIR, "A_cbuffer_stability.json"), "w") as f:
    json.dump(cbuffer_results, f, indent=2, default=str)
log(f"wrote A_cbuffer_stability.json")

# =====================================================================
# B. DESCRIPTOR TABLE per-eye check (use CAPTURE_A SLW composite pair)
# =====================================================================
log("\n=== B. DESCRIPTOR TABLE per-eye ===")
cap, controller = _lib.open_capture(CAPTURE_A)
try:
    L_slw, R_slw = 20876, 20954
    log(f"  SLW composite pair: L={L_slw} R={R_slw}")
    l_root = collect_all_root_state(controller, L_slw)
    r_root = collect_all_root_state(controller, R_slw)
    table_diff = []
    for lp, rp in zip(l_root, r_root):
        if "table" not in lp or "table" not in rp:
            continue
        lt = lp["table"]; rt = rp["table"]
        if lt["heap"] == rt["heap"] and lt["heapByteOffset"] == rt["heapByteOffset"]:
            kind = "shared"
        else:
            kind = "per_eye"
        table_diff.append({
            "rootIndex": lp["index"],
            "visibility": lp["visibility"],
            "register": lp["register"],
            "L_heap": lt["heap"], "L_offset": lt["heapByteOffset"],
            "R_heap": rt["heap"], "R_offset": rt["heapByteOffset"],
            "mode": kind,
            "offsetDelta": rt["heapByteOffset"] - lt["heapByteOffset"] if kind == "per_eye" else 0,
        })
    log(f"  descriptor tables: {len(table_diff)} total")
    for t in table_diff:
        log(f"    [{t['rootIndex']}] {t['visibility']} reg{t['register']}: "
            f"L=heap{t['L_heap']}+{t['L_offset']} R=heap{t['R_heap']}+{t['R_offset']}  "
            f"mode={t['mode']}  delta={t['offsetDelta']}")
    per_eye_tables = [t for t in table_diff if t['mode'] == 'per_eye']
    log(f"  PER-EYE tables: {len(per_eye_tables)}")
    log(f"  SHARED tables: {len(table_diff) - len(per_eye_tables)}")
    if not per_eye_tables:
        log("  VERDICT: descriptor tables are SHARED — UEVR only needs cbuffer swaps.")
    else:
        log("  VERDICT: descriptor tables are PER-EYE — UEVR needs both cbuffer AND table swaps.")
    with open(os.path.join(OUT_DIR, "B_descriptor_tables.json"), "w") as f:
        json.dump(table_diff, f, indent=2, default=str)
    log(f"wrote B_descriptor_tables.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

# =====================================================================
# C. OTHER SHARED-UAV CONSUMERS — quick check
# =====================================================================
log("\n=== C. OTHER SHARED-UAV CONSUMERS ===")
cap, controller = _lib.open_capture(CAPTURE_A)
try:
    # Find PS consumers of each shared UAV
    consumers = {}
    for res in OTHER_SHARED_UAVS:
        # Resolve resource ID -> object
        target = None
        for r in controller.GetResources():
            if _lib.resource_id_str(r.resourceId) == res:
                target = r.resourceId
                break
        if target is None:
            log(f"  {res}: resource not found")
            consumers[res] = {"error": "not_found"}
            continue
        usage = controller.GetUsage(target)
        ps_consumers = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "PS_Resource" in kind or "VS_Resource" in kind:
                ps_consumers.append({"eventId": int(u.eventId), "usage": kind})
        consumers[res] = {
            "totalUsages": len(usage),
            "psConsumerCount": len(ps_consumers),
            "psConsumers": ps_consumers[:20],
        }
        log(f"  {res}: {len(ps_consumers)} PS/VS consumers (of {len(usage)} total usages)")
        for c in ps_consumers[:5]:
            log(f"    eid {c['eventId']} usage={c['usage']}")
    with open(os.path.join(OUT_DIR, "C_other_shared_consumers.json"), "w") as f:
        json.dump(consumers, f, indent=2, default=str)
    log(f"wrote C_other_shared_consumers.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("\n=== DONE ===")
os._exit(0)
