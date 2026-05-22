"""UEVR followup round 2 — cbuffer stability, compute eye classifier,
LightScatteringCS chain trace.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_uevr_followup_2"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

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


cap, controller = _lib.open_capture(CAPTURE)
try:
    # =================================================================
    # STAGE E: trace LightScatteringCS chain
    # =================================================================
    log("=== STAGE E: trace LightScatteringCS chain ===")
    # The fog froxel (28115) is read by LightScatteringCS at event 20206.
    # That CS writes some output texture. Find what it writes, then trace
    # what PS shaders consume that.

    LIGHT_SCATTERING_CS_EID = 20206
    log(f"  inspecting LightScatteringCS at event {LIGHT_SCATTERING_CS_EID}...")
    controller.SetFrameEvent(LIGHT_SCATTERING_CS_EID, True)
    pipe = controller.GetPipelineState()
    ls_uavs = []
    try:
        for u in pipe.GetReadWriteResources(rd.ShaderStage.Compute, False):
            d = u.descriptor
            if d is None or d.resource is None:
                continue
            ls_uavs.append({
                "register": int(u.access.index),
                "resource": res_id(d.resource),
            })
    except Exception:
        pass
    log(f"  LightScatteringCS UAVs: {ls_uavs}")

    # For each UAV, find PS consumers
    log("  tracing PS consumers of each LightScatteringCS output...")
    ls_chain = {}
    for uav in ls_uavs:
        rid_str = uav["resource"]
        target = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == rid_str:
                target = r.resourceId
                break
        if target is None:
            continue
        usage = controller.GetUsage(target)
        ps_consumers = []
        for u in usage:
            k = str(u.usage).split(".")[-1]
            if "PS_Resource" in k or "VS_Resource" in k:
                ps_consumers.append({"eventId": int(u.eventId), "usage": k})
        # Resource details
        details = {}
        for t in controller.GetTextures():
            if res_id(t.resourceId) == rid_str:
                details = {
                    "type": "Texture",
                    "width": int(t.width), "height": int(t.height),
                    "depth": int(t.depth),
                    "format": str(t.format.Name()),
                }
                break
        ls_chain[rid_str] = {
            "uav_register": uav["register"],
            "details": details,
            "psConsumerCount": len(ps_consumers),
            "psConsumers": ps_consumers[:10],
        }
        log(f"    {rid_str}: {details}  PS readers: {len(ps_consumers)}")
        for c in ps_consumers[:3]:
            log(f"      eid {c['eventId']} usage={c['usage']}")
    with open(os.path.join(OUT_DIR, "E_light_scattering_chain.json"), "w") as f:
        json.dump(ls_chain, f, indent=2, default=str)
    log("  wrote E_light_scattering_chain.json")

    # For PS consumers, identify them — entry name + which eye + bound t5/t8/t9
    log("\n  inspecting PS consumers of LightScatteringCS output...")
    all_ps_consumers = []
    for rid_str, chain_info in ls_chain.items():
        for c in chain_info["psConsumers"]:
            all_ps_consumers.append({"resource": rid_str, **c})
    unique_consumers = {c["eventId"]: c for c in all_ps_consumers}
    log(f"  {len(unique_consumers)} unique PS consumer events")
    consumer_details = []
    for eid, c in list(unique_consumers.items())[:30]:
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                ps_entry = str(refl.entryPoint) if refl else None
            except Exception:
                ps_entry = None
            vp_x = None
            try:
                if len(d3d12.rasterizer.viewports) > 0:
                    vp_x = float(d3d12.rasterizer.viewports[0].x)
            except Exception:
                pass
            consumer_details.append({
                "eventId": eid, "resource": c["resource"],
                "psEntry": ps_entry, "viewport_x": vp_x,
            })
        except Exception:
            pass
    eye_split = {"left": 0, "right": 0, "unknown": 0}
    for c in consumer_details:
        if c.get("viewport_x") is None:
            eye_split["unknown"] += 1
        elif c["viewport_x"] >= 400:
            eye_split["right"] += 1
        else:
            eye_split["left"] += 1
    log(f"  consumer eye split: {eye_split}")
    from collections import Counter
    entry_counts = Counter(c.get("psEntry") for c in consumer_details)
    log(f"  consumer PS entries: {dict(entry_counts)}")
    with open(os.path.join(OUT_DIR, "E_ls_consumer_details.json"), "w") as f:
        json.dump(consumer_details, f, indent=2, default=str)

    # =================================================================
    # STAGE F: compute-pipeline eye classifier (by UAV target side)
    # =================================================================
    log("\n=== STAGE F: compute eye classifier for HZB / UWE fog ===")
    # For HZB (29816) — 8 CS UAV writers. For each writer event, look at the
    # OTHER bound UAVs (not just 29816). Some of those other UAVs might also
    # have per-eye allocation that we can use to infer eye.
    COMPUTE_CONSUMERS_TO_CLASSIFY = {
        "ResourceId::29816": "HZB",
        "ResourceId::30092": "UWE fog denoise out 1",
        "ResourceId::30095": "UWE fog denoise out 2",
    }
    compute_classification = {}
    for res_target, label in COMPUTE_CONSUMERS_TO_CLASSIFY.items():
        log(f"\n  {res_target} ({label}):")
        rid = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == res_target:
                rid = r.resourceId; break
        if rid is None:
            continue
        usage = controller.GetUsage(rid)
        compute_events = []
        for u in usage:
            k = str(u.usage).split(".")[-1]
            if "CS_RWResource" in k or "CS_Resource" in k:
                compute_events.append({"eventId": int(u.eventId), "usage": k})
        # For each, inspect bound CS shader + cbuffers to infer eye
        # Heuristic: read the CS cbuffer (PS b0 / CS b0 = View). If it
        # matches left-eye offset (3166208) it's a left-eye CS; if right
        # (3155968) it's right-eye.
        LEFT_VIEW_OFFSET = 3166208
        RIGHT_VIEW_OFFSET = 3155968
        per_event_class = []
        for ev in compute_events:
            eid = ev["eventId"]
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
                pipe = controller.GetPipelineState()
                refl = None
                try:
                    refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
                except Exception:
                    pass
                cs_entry = str(refl.entryPoint) if refl else None
                # Look at root CBVs for View cbuffer offset
                view_offset = None
                try:
                    for p in d3d12.rootSignature.parameters:
                        try:
                            d = p.descriptor
                        except Exception:
                            continue
                        if d is None or getattr(d, "resource", None) is None:
                            continue
                        if res_id(d.resource) == "ResourceId::29343":
                            view_offset = int(d.byteOffset)
                            break  # take first
                except Exception:
                    pass
                if view_offset == LEFT_VIEW_OFFSET:
                    eye = "left"
                elif view_offset == RIGHT_VIEW_OFFSET:
                    eye = "right"
                elif view_offset is None:
                    eye = "no_view_cbv"
                else:
                    eye = f"unknown_offset_{view_offset}"
                per_event_class.append({
                    "eventId": eid, "usage": ev["usage"],
                    "csEntry": cs_entry, "viewOffset": view_offset, "eye": eye,
                })
            except Exception:
                pass
        compute_classification[res_target] = {
            "label": label, "events": per_event_class,
        }
        from collections import Counter
        eye_counts = Counter(e["eye"] for e in per_event_class)
        log(f"    eye classification: {dict(eye_counts)}")
        for e in per_event_class[:8]:
            log(f"    eid {e['eventId']} {e['usage']:25} entry={e['csEntry']} eye={e['eye']}")
    with open(os.path.join(OUT_DIR, "F_compute_eye_class.json"), "w") as f:
        json.dump(compute_classification, f, indent=2, default=str)
    log("  wrote F_compute_eye_class.json")

    # Verdict per subsystem
    log("\n  === VERDICTS ===")
    for res, info in compute_classification.items():
        from collections import Counter
        c = Counter(e["eye"] for e in info["events"])
        l = c.get("left", 0); r = c.get("right", 0)
        if r == 0 and l > 0:
            v = "❗ ASYMMETRIC (left-only) — same bug as water"
        elif r > 0 and l > 0:
            v = "✓ symmetric"
        else:
            v = "unclassified"
        log(f"    {res} ({info['label']}): {v} (L={l} R={r})")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
