"""Two-part analysis for UEVR's shadow refactor:

A. From the no-UEVR baseline: for every aliased bucket containing fog-shaped
   3D resources, list ALL events that write to ANY resource in the bucket.
   That tells UEVR exactly when to do shadow population (option A: snapshot
   at aliasing barrier; option C: copy at consumer-draw time).

B. From the UEVR-ON capture: find pso3069 (PS hash starting with 166dba88).
   Locate left + right instances. Dump their full PS SRV bindings at t5/t8/t9
   and identify each resource (dimensions, format, placed-vs-committed).
   Tells UEVR whether the right-eye pso3069 reads 3D-aliased fog (in registry)
   or 2D-committed fog (not in registry).
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_shadow_pso3069"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE_BASELINE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
CAPTURE_UEVR_ON = r"E:\Github\Subnautica 2\captures\sn2_20260516_081926_frame1255.rdc"


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def real_writers(controller, target_rid_str):
    """Filtered writers (exclude Discard/Barrier lifecycle markers)."""
    target = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == target_rid_str:
            target = r.resourceId; break
    if target is None:
        return []
    out = []
    for u in controller.GetUsage(target):
        kind = str(u.usage).split(".")[-1]
        if "Discard" in kind or "Barrier" in kind:
            continue
        if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind \
           or "CopyDst" in kind:
            out.append({"eventId": int(u.eventId), "usage": kind})
    return out


# =====================================================================
# PART A — baseline: aliased fog-bucket writers
# =====================================================================
log("=== PART A: aliased fog-bucket writers (baseline) ===")
heap_map = json.load(open(r"E:\tmp_dir\sn2_aliasing_verify\placed_heap_map.json"))
aliased_groups = heap_map.get("aliasedGroups", [])
log(f"  loaded {len(aliased_groups)} aliased buckets")

# Filter to buckets containing fog-shaped 3D textures
fog_buckets = []
for g in aliased_groups:
    dims = g.get("dimensions") or []
    has_fog_3d = False
    for d in dims:
        if not isinstance(d, dict):
            continue
        if d.get("is3D") and d.get("format", "") in ("R11G11B10_FLOAT", "R16G16B16A16_FLOAT"):
            has_fog_3d = True
            break
    if has_fog_3d:
        fog_buckets.append(g)
log(f"  buckets with fog-shaped 3D resources: {len(fog_buckets)}")

# For each fog bucket, list its members + writers
cap_b, controller_b = _lib.open_capture(CAPTURE_BASELINE)
try:
    bucket_writers = []
    for g in fog_buckets:
        bucket = {"heap": g["heap"], "offset": g["offset"], "members": [], "writers": []}
        for rid, dim in zip(g["resources"], g["dimensions"]):
            if not isinstance(dim, dict):
                dim = {}
            member = {"resource": rid, "dim": dim}
            ws = real_writers(controller_b, rid)
            member["writers"] = ws
            bucket["members"].append(member)
            bucket["writers"].extend([(rid, w) for w in ws])
        bucket_writers.append(bucket)
    log(f"\n  fog bucket writer summary (first 10):")
    for bucket in bucket_writers[:10]:
        log(f"\n  heap {bucket['heap']} +{bucket['offset']}:")
        for m in bucket["members"]:
            d = m["dim"]
            if d.get("w"):
                log(f"    {m['resource']} ({d.get('w')}x{d.get('h')}x{d.get('d')} {d.get('format')}): "
                    f"{len(m['writers'])} real writers")
                for w in m["writers"][:3]:
                    log(f"      eid {w['eventId']} usage={w['usage']}")
            else:
                log(f"    {m['resource']} (no dim info)")
    with open(os.path.join(OUT_DIR, "A_fog_bucket_writers.json"), "w") as f:
        json.dump(bucket_writers, f, indent=2, default=str)
    log(f"\n  wrote A_fog_bucket_writers.json")
finally:
    controller_b.Shutdown()
    cap_b.Shutdown()

# =====================================================================
# PART B — UEVR-ON: pso3069 right-eye SRV bindings
# =====================================================================
log("\n=== PART B: pso3069 right-eye binding analysis (UEVR-on capture) ===")
if not os.path.isfile(CAPTURE_UEVR_ON):
    log(f"  UEVR-on capture not found at {CAPTURE_UEVR_ON} — skipping Part B")
else:
    cap_u, controller_u = _lib.open_capture(CAPTURE_UEVR_ON)
    try:
        # Find pso3069 events by PS hash prefix
        PSO3069_HASH_PREFIX = "166dba88"
        log(f"  searching for events with PS hash prefix {PSO3069_HASH_PREFIX}...")
        pso_events = []
        n_actions = 0
        for a in _lib.walk_actions(controller_u):
            flags = int(a.flags)
            if not (flags & int(rd.ActionFlags.Drawcall)):
                continue
            n_actions += 1
            eid = int(a.eventId)
            try:
                controller_u.SetFrameEvent(eid, True)
                pipe = controller_u.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            except Exception:
                continue
            if refl and len(refl.rawBytes) > 0:
                h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                if h.lower().startswith(PSO3069_HASH_PREFIX.lower()):
                    pso_events.append({"eventId": eid, "psHash": h,
                                       "numIndices": int(a.numIndices)})
                    if len(pso_events) >= 30:
                        break
            if n_actions > 5000:
                # safety cap — pso3069 is in late-frame opaque pass, usually <5000 draws in
                break
        log(f"  scanned {n_actions} draws, found {len(pso_events)} pso3069 instances")
        for p in pso_events[:10]:
            log(f"    eid {p['eventId']} numIndices={p['numIndices']}")

        # For each, dump PS SRV bindings at registers t5, t6, t7, t8, t9
        if pso_events:
            log(f"\n  dumping PS SRV bindings t5-t9 for first 6 pso3069 events:")
            details = []
            tex_index = {res_id(t.resourceId): t for t in controller_u.GetTextures()}
            for p in pso_events[:6]:
                eid = p["eventId"]
                controller_u.SetFrameEvent(eid, True)
                pipe = controller_u.GetPipelineState()
                bindings = []
                try:
                    for u in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
                        d = u.descriptor
                        if d is None or d.resource is None:
                            continue
                        reg = int(u.access.index)
                        if reg not in (5, 6, 7, 8, 9):
                            continue
                        rid = res_id(d.resource)
                        t = tex_index.get(rid)
                        if t is None:
                            kind = "(not a texture)"
                        else:
                            depth = int(t.depth)
                            kind = f"{int(t.width)}x{int(t.height)}x{depth} {t.format.Name()}"
                            if depth > 1:
                                kind += " [3D]"
                            else:
                                kind += " [2D]"
                        bindings.append({
                            "register": reg, "resource": rid, "desc": kind,
                            "descStoreHeap": res_id(u.access.descriptorStore),
                            "descStoreOffset": int(u.access.byteOffset),
                        })
                except Exception as e:
                    log(f"    eid {eid}: ERROR {e}")
                bindings.sort(key=lambda b: b["register"])
                log(f"\n    eid {eid} ({p['numIndices']} idx):")
                for b in bindings:
                    log(f"      t{b['register']} = {b['resource']}  [{b['desc']}]  "
                        f"heap={b['descStoreHeap']}+{b['descStoreOffset']}")
                details.append({"eventId": eid, "psHash": p["psHash"],
                                "numIndices": p["numIndices"], "bindings": bindings})

            # Identify 3D vs 2D
            for d in details:
                has_3d = any("[3D]" in b["desc"] for b in d["bindings"])
                has_2d = any("[2D]" in b["desc"] for b in d["bindings"])
                d["has3D_fog_input"] = has_3d
                d["has2D_fog_input"] = has_2d
            with open(os.path.join(OUT_DIR, "B_pso3069_bindings.json"), "w") as f:
                json.dump(details, f, indent=2, default=str)
            log(f"\n  wrote B_pso3069_bindings.json")

            # Summary verdict
            log("\n  === VERDICT ===")
            for d in details:
                v = []
                if d["has3D_fog_input"]: v.append("3D fog")
                if d["has2D_fog_input"]: v.append("2D fog/textures")
                log(f"    eid {d['eventId']}: reads {' + '.join(v) or 'no fog at t5-t9'}")
    finally:
        controller_u.Shutdown()
        cap_u.Shutdown()

log("DONE")
os._exit(0)
