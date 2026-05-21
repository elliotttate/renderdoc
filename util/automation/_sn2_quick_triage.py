"""Fast-lane SN2 right-eye investigation.

Skips the per-action descriptor poll (which is slow on big captures) and
only does the cheap chunk-based analysis. Good first run to see whether
the capture even has the expected right-eye basepass MainPS draws, and to
get the chunk-level descriptor-write log.

For the full per-action snapshot, follow up with _run_sn2_investigation.py.
"""

import json
import os
import sys
import traceback

LOG_DIR = os.environ.get("SN2_INVESTIGATION_OUT", r"E:\tmp_dir\sn2_invest_triage")
CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)
PSO_HASH_PREFIX = os.environ.get("SN2_PSO_HASH_PREFIX", "166dba88")

os.makedirs(LOG_DIR, exist_ok=True)
PROGRESS = os.path.join(LOG_DIR, "_progress.log")


def log(msg):
    line = f"[sn2-triage] {msg}"
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    try:
        print(line, flush=True)
    except Exception:
        pass


with open(PROGRESS, "w", encoding="utf-8") as f:
    pass

log(f"capture: {CAPTURE}")
log(f"out:     {LOG_DIR}")

if not os.path.isfile(CAPTURE):
    log(f"FATAL: capture not found")
    os._exit(2)

sys.path.insert(0, r"E:\Github\renderdoc")

try:
    from util.automation import _lib, d3d12_copy_descriptors
    import renderdoc as rd
except Exception:
    log(traceback.format_exc())
    os._exit(3)


def write_json(name, payload):
    full = os.path.join(LOG_DIR, name)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"wrote {name}")


# 1. Cheap chunk-level descriptor write log.
log("Step 1: descriptor write log (chunks)...")
try:
    copy_log = d3d12_copy_descriptors.extract(CAPTURE)
    write_json("descriptor_copy_log.json", copy_log)
    log(f"  {copy_log.get('total', 0)} descriptor-write chunks; "
        f"summary: {copy_log.get('summary', {})}")
except Exception:
    log("Step 1 failed:\n" + traceback.format_exc())

# 2. Walk actions once, find right-eye basepass MainPS draws.
log(f"Step 2: scanning actions for right-eye basepass PS hash prefix={PSO_HASH_PREFIX}...")
try:
    cap, controller = _lib.open_capture(CAPTURE)
    try:
        hits_right = []
        hits_left = []
        for a in _lib.walk_actions(controller):
            if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            eid = int(a.eventId)
            try:
                controller.SetFrameEvent(eid, True)
            except Exception:
                continue
            pipe = controller.GetPipelineState()
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            except Exception:
                refl = None
            if refl is None or len(refl.rawBytes) == 0:
                continue
            h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
            if not h.lower().startswith(PSO_HASH_PREFIX.lower()):
                continue
            # Classify by viewport.x: if right half of an at-least-double-wide RT
            # OR x >= some big-half threshold, call it right-eye.
            d3d12 = None
            try:
                d3d12 = controller.GetD3D12PipelineState()
            except Exception:
                pass
            vp = None
            rt_w = 0
            if d3d12 is not None:
                if len(d3d12.rasterizer.viewports) > 0:
                    v = d3d12.rasterizer.viewports[0]
                    vp = (float(v.x), float(v.y), float(v.width), float(v.height))
                if len(d3d12.outputMerger.renderTargets) > 0:
                    rt = d3d12.outputMerger.renderTargets[0]
                    for t in controller.GetTextures():
                        if t.resourceId == rt.resource:
                            rt_w = int(t.width)
                            break
            entry = {"eventId": eid, "psHash": h, "viewport": vp, "rtWidth": rt_w}
            if vp is None or rt_w == 0:
                continue
            if vp[0] >= rt_w / 2 - 1.0:
                hits_right.append(entry)
            else:
                hits_left.append(entry)
        write_json("right_eye_basepass_hits.json", hits_right)
        write_json("left_eye_basepass_hits.json", hits_left)
        log(f"  right-eye: {len(hits_right)}, left-eye: {len(hits_left)}")

        # 3. State snapshot at the first right-eye basepass draw.
        if hits_right:
            target = hits_right[0]["eventId"]
            log(f"Step 3: state at right-eye event {target}...")
            state = _lib.collect_state_at_event(controller, target)
            write_json(f"state_at_event_{target}.json", state)
            t5 = None
            for b in state.get("bindings", []):
                if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                        (b.get("type") or "").startswith("Read"):
                    t5 = b
                    break
            log(f"  PS t5: {t5}")
        if hits_left:
            target = hits_left[0]["eventId"]
            log(f"Step 3b: state at left-eye event {target}...")
            state = _lib.collect_state_at_event(controller, target)
            write_json(f"state_at_event_{target}_left.json", state)
            t5 = None
            for b in state.get("bindings", []):
                if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                        (b.get("type") or "").startswith("Read"):
                    t5 = b
                    break
            log(f"  PS t5: {t5}")
    finally:
        controller.Shutdown()
        cap.Shutdown()
except Exception:
    log("Step 2 failed:\n" + traceback.format_exc())

log("done.")
os._exit(0)
