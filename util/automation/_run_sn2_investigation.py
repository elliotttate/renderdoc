"""Apply the renderdoc fork's automation modules to the SN2 right-eye bug.

Run via qrenderdoc's embedded Python:

    qrenderdoc.exe --python E:/Github/renderdoc/util/automation/_run_sn2_investigation.py

Outputs a folder of JSON artifacts at $SN2_INVESTIGATION_OUT (default
E:/tmp_dir/sn2_investigation) containing:

  - eye_classification.json
      All draw/dispatch events labelled left / right / unknown, with viewport
      and render-target evidence.
  - right_eye_basepass_hits.json
      Right-eye draws whose PS bytecode hash starts with PSO_HASH_PREFIX
      (default 166dba88, the basepass MainPS for SN2's cave-opening).
  - state_at_event_<eid>.json
      Full pipeline state snapshot at the first right-eye basepass draw —
      root signature, descriptor heaps, every bound resource.
  - resource_lineage_<resource>.json
      Read/write history of the resource the broken draw samples at t5.
  - descriptor_copy_log.json
      Every D3D12 descriptor mutation chunk (CopyDescriptors[Simple],
      CreateXxxView) with destination heap + slot resolved via PortableHandle.
  - descriptor_history.json
      Polled per-action snapshot of what's actually in every consumed
      descriptor slot at every draw/dispatch.

Defaults assume the May 16 vanilla-stereo capture
``sn2_nouevr_20260516_170001_frame610.rdc``. Override the capture path via
the env var SN2_INVESTIGATION_CAPTURE.
"""

import json
import os
import sys
import traceback

LOG_DIR = os.environ.get("SN2_INVESTIGATION_OUT", r"E:\tmp_dir\sn2_investigation")
CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)
PSO_HASH_PREFIX = os.environ.get("SN2_PSO_HASH_PREFIX", "166dba88")

os.makedirs(LOG_DIR, exist_ok=True)
PROGRESS = os.path.join(LOG_DIR, "_progress.log")


def log(msg):
    line = f"[sn2-invest] {msg}"
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
    from util.automation import (
        _lib,
        eye_classifier,
        descriptor_history,
        d3d12_copy_descriptors,
        resource_lineage,
    )
    import renderdoc as rd
except Exception:
    log(traceback.format_exc())
    os._exit(3)


def write_json(name, payload):
    full = os.path.join(LOG_DIR, name)
    with open(full, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"wrote {name}")


# -------------------------------------------------------------------------
# Step 1: eye classification
# -------------------------------------------------------------------------
try:
    log("Step 1: classifying events by eye...")
    eye = eye_classifier.classify_capture(CAPTURE, {"mode": "auto"})
    write_json("eye_classification.json", eye)
    right_eye = [e for e in eye["events"] if (e.get("eye") or "").lower() == "right"]
    log(f"  {len(right_eye)} right-eye events / {len(eye['events'])} total")
except Exception:
    log("Step 1 failed:\n" + traceback.format_exc())
    right_eye = []


# -------------------------------------------------------------------------
# Steps 2-4: identify right-eye basepass MainPS draws, snapshot state, walk lineage
# -------------------------------------------------------------------------
hits = []
target_eid = None
t5_resource = None
try:
    log(f"Step 2: finding right-eye basepass draws (PS hash prefix={PSO_HASH_PREFIX})...")
    cap, controller = _lib.open_capture(CAPTURE)
    try:
        for ev in right_eye:
            eid = int(ev["eventId"])
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
            if h.lower().startswith(PSO_HASH_PREFIX.lower()):
                hits.append({"eventId": eid, "psHash": h, "eye": ev["eye"]})
        write_json("right_eye_basepass_hits.json", hits)
        log(f"  {len(hits)} right-eye basepass draws found")

        if hits:
            target_eid = hits[0]["eventId"]
            log(f"Step 3: dumping state at event {target_eid}...")
            state = _lib.collect_state_at_event(controller, target_eid)
            write_json(f"state_at_event_{target_eid}.json", state)

            t5_binding = None
            for b in state.get("bindings", []):
                if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                        (b.get("type") or "").startswith("Read"):
                    t5_binding = b
                    break
            log(f"  t5 binding: {t5_binding}")
            if t5_binding and t5_binding.get("resource"):
                t5_resource = t5_binding["resource"]

        # Also classify and snapshot a corresponding left-eye basepass draw,
        # for side-by-side comparison.
        left_eye = [e for e in eye["events"] if (e.get("eye") or "").lower() == "left"]
        left_hits = []
        for ev in left_eye:
            eid = int(ev["eventId"])
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
            if h.lower().startswith(PSO_HASH_PREFIX.lower()):
                left_hits.append({"eventId": eid, "psHash": h, "eye": ev["eye"]})
                if len(left_hits) >= 1:
                    break
        write_json("left_eye_basepass_hits.json", left_hits)
        if left_hits:
            left_eid = left_hits[0]["eventId"]
            log(f"  left-eye baseline event {left_eid}")
            state_l = _lib.collect_state_at_event(controller, left_eid)
            write_json(f"state_at_event_{left_eid}_left.json", state_l)
    finally:
        controller.Shutdown()
        cap.Shutdown()
except Exception:
    log("Step 2-3 failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Step 4: resource lineage for the t5-bound resource
# -------------------------------------------------------------------------
try:
    if t5_resource:
        log(f"Step 4: resource lineage for {t5_resource}...")
        lin = resource_lineage.lineage(CAPTURE, t5_resource)
        write_json(f"resource_lineage_{t5_resource.replace(':', '_')}.json", lin)
    else:
        log("Step 4 skipped: no t5 resource identified")
except Exception:
    log("Step 4 failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Step 5: full descriptor mutation log (raw chunks)
# -------------------------------------------------------------------------
try:
    log("Step 5: extracting descriptor write log from structured chunks "
        "(this is slow on big captures)...")
    copy_log = d3d12_copy_descriptors.extract(CAPTURE)
    write_json("descriptor_copy_log.json", copy_log)
    log(f"  {copy_log.get('total', 0)} descriptor-write chunks; "
        f"summary: {copy_log.get('summary', {})}")
except Exception:
    log("Step 5 failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Step 6: per-action descriptor history (polled)
# -------------------------------------------------------------------------
try:
    log("Step 6: polling descriptor history per action (slow)...")
    hist = descriptor_history.descriptor_history(CAPTURE, mode="slots-from-bindings")
    write_json("descriptor_history.json", hist)
    log(f"  {hist['summary'].get('writeCount', 0)} resolved per-event writes")
except Exception:
    log("Step 6 failed:\n" + traceback.format_exc())


log("done.")
os._exit(0)
