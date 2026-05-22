"""Verify: in the UEVR-on capture, does pso3069's right-eye instance
bind the same 24x24x24 R16G16B16A16F TranslucentLightingVolume textures
at PS t3 + t5 that we identified as the bug surface in the live no-UEVR
capture?

Memory-light: walks actions only from event 20000+ (pso3069 is in the
late-frame opaque pass). Caps at first 6 matches.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "uevr_on_pso3069.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_20260516_081926_frame1255.rdc"
PSO3069_PS_HASH_PREFIX = "166dba88"


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


log(f"open_capture: {CAPTURE}")
cap, controller = _lib.open_capture(CAPTURE)
log("opened")
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # Find pso3069 instances, capped, only past event 20000
    pso_events = []
    last_log_eid = 0
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & int(rd.ActionFlags.Drawcall)):
            continue
        eid = int(a.eventId)
        if eid < 20000:
            continue
        if eid - last_log_eid > 500:
            log(f"  ... scanning at eid {eid}, {len(pso_events)} pso3069 found so far")
            last_log_eid = eid
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        except Exception:
            continue
        if not refl or len(refl.rawBytes) == 0:
            continue
        h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
        if not h.lower().startswith(PSO3069_PS_HASH_PREFIX.lower()):
            continue
        # Classify eye via viewport.x
        d3d12 = controller.GetD3D12PipelineState()
        vp_x = None
        try:
            if len(d3d12.rasterizer.viewports) > 0:
                vp_x = float(d3d12.rasterizer.viewports[0].x)
        except Exception:
            pass
        pso_events.append({"eventId": eid, "psHash": h, "vp_x": vp_x,
                            "numIndices": int(a.numIndices)})
        if len(pso_events) >= 12:
            break

    log(f"\nfound {len(pso_events)} pso3069 instances")
    for p in pso_events:
        log(f"  eid {p['eventId']}  vp_x={p['vp_x']}  numIndices={p['numIndices']}")

    # Pick one LEFT (vp_x < some threshold) and one RIGHT (vp_x >= threshold).
    # Capture is at 1280x720 typically — half_w = 640.
    left = next((p for p in pso_events if p['vp_x'] is not None and p['vp_x'] < 200), None)
    right = next((p for p in pso_events if p['vp_x'] is not None and p['vp_x'] >= 600), None)
    if not left:
        log("  no LEFT pso3069 candidate")
        left = pso_events[0] if pso_events else None
    if not right:
        log("  no RIGHT pso3069 candidate — falling back to second instance")
        right = pso_events[1] if len(pso_events) > 1 else None

    # For each, dump PS SRV bindings t3 + t5 + t0..t9 for context
    def dump_psbindings(eid, label):
        controller.SetFrameEvent(eid, True)
        pipe = controller.GetPipelineState()
        bindings = []
        try:
            for u in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
                d = u.descriptor
                if d is None or d.resource is None:
                    continue
                reg = int(u.access.index)
                if reg > 14:
                    continue
                rid = res_id(d.resource)
                tex = textures_by_id.get(rid)
                if tex is None:
                    desc = "(not a texture)"
                else:
                    desc = (f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)} "
                            f"{tex.format.Name()}")
                    if int(tex.depth) > 1:
                        desc += " [3D]"
                bindings.append({"register": reg, "resource": rid, "desc": desc})
        except Exception as e:
            log(f"  binding read error: {e}")
        bindings.sort(key=lambda b: b['register'])
        log(f"\n  {label} pso3069 (eid {eid}, vp_x={pso_events[0]['vp_x'] if label == 'LEFT' else 'n/a'}):")
        for b in bindings:
            star = " ★" if b['register'] in (3, 5) else ""
            log(f"    t{b['register']:2}{star} = {b['resource']}  [{b['desc']}]")
        return bindings

    left_b = right_b = None
    if left:
        left_b = dump_psbindings(left['eventId'], "LEFT")
    if right:
        right_b = dump_psbindings(right['eventId'], "RIGHT")

    # Compare t3 and t5 between left/right
    def get_at(bindings, reg):
        return next((b for b in (bindings or []) if b['register'] == reg), None)

    log("\n=== T3/T5 COMPARISON ===")
    if left_b and right_b:
        for slot in (3, 5):
            l = get_at(left_b, slot)
            r = get_at(right_b, slot)
            same = (l and r and l['resource'] == r['resource'])
            same_str = "SAME RID" if same else "DIFFERENT RIDs"
            log(f"  t{slot}: LEFT={l['resource'] if l else 'N/A'} "
                f"vs RIGHT={r['resource'] if r else 'N/A'}  -> {same_str}")
            if l: log(f"        LEFT desc: {l['desc']}")
            if r: log(f"        RIGHT desc: {r['desc']}")

    # Check if any t3/t5 binding is a 24x24x24 R16G16B16A16F volume
    log("\n=== Is the bug surface (24x24x24 R16G16B16A16F) bound here? ===")
    found_target_shape = False
    for label, bindings in (("LEFT", left_b), ("RIGHT", right_b)):
        if not bindings:
            continue
        for slot in (3, 5):
            b = get_at(bindings, slot)
            if not b:
                continue
            if "24x24x24" in b['desc'] and "R16G16B16A16" in b['desc']:
                found_target_shape = True
                log(f"  ✅ {label} t{slot} binds {b['resource']} matching target shape")
    if not found_target_shape:
        log(f"  ❌ Neither LEFT nor RIGHT pso3069 binds 24x24x24 R16G16B16A16F at t3 or t5")
        log(f"     This means UEVR-on uses different bindings for the visible-bug surface.")

    out = {
        "pso_events": pso_events,
        "left_event": left,
        "right_event": right,
        "left_bindings": left_b,
        "right_bindings": right_b,
        "target_shape_found": found_target_shape,
    }
    with open(os.path.join(OUT_DIR, "uevr_on_pso3069.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nwrote uevr_on_pso3069.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
