"""Stage D — for each shared UAV's consumers, identify the eye-assignment
so we know if the same mesh-batch-level bug applies to HZB / Lumen / UWE fog.

For each consumer event:
  - Get its bound RTV (if any) — tells us which scene region it renders to
  - Get its viewport.x — tells us if it's targeting right half (>= half_width)
  - Classify as left or right by viewport position
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_uevr_followup"
LOG = os.path.join(OUT_DIR, "D_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# Load Stage C output to get the consumer events
C_data = json.load(open(os.path.join(OUT_DIR, "C_other_shared_consumers.json")))


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    results = {}
    for resource, info in C_data.items():
        if "error" in info:
            continue
        label = info["label"]
        all_consumers = info.get("psConsumers", []) + info.get("csConsumers", [])
        if not all_consumers:
            continue
        log(f"\n=== {resource} ({label}): {len(all_consumers)} consumers ===")
        consumer_eye = []
        for c in all_consumers:
            eid = c["eventId"]
            usage = c["usage"]
            try:
                controller.SetFrameEvent(int(eid), True)
                d3d12 = controller.GetD3D12PipelineState()
                # Get viewport.x
                vp_x = None
                vp_w = None
                try:
                    if len(d3d12.rasterizer.viewports) > 0:
                        v = d3d12.rasterizer.viewports[0]
                        vp_x = float(v.x); vp_w = float(v.width)
                except Exception:
                    pass
                # Get bound RT shape to compute half-width
                rt_w = None
                try:
                    if len(d3d12.outputMerger.renderTargets) > 0:
                        rt = d3d12.outputMerger.renderTargets[0]
                        rid = res_id(rt.resource)
                        if rid:
                            for t in controller.GetTextures():
                                if str(t.resourceId) == rid:
                                    rt_w = int(t.width)
                                    break
                except Exception:
                    pass
                # For compute shaders no RT — use viewport-only heuristic
                # If vp_x > 0 and vp_w > 0, classify by absolute position
                if vp_x is None or vp_w is None:
                    eye = "compute_no_viewport"
                elif vp_x == 0 and vp_w > 600:
                    eye = "left_or_full"
                elif vp_x >= 400:
                    eye = "right"
                else:
                    eye = "left"
                # Also grab the bound PS shader entry if any
                ps_entry = None
                cs_entry = None
                try:
                    ps_refl = d3d12.pixelShader if hasattr(d3d12, "pixelShader") else None
                except Exception:
                    ps_refl = None
                try:
                    pipe = controller.GetPipelineState()
                    refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel) if "PS" in usage else \
                           pipe.GetShaderReflection(rd.ShaderStage.Compute) if "CS" in usage else None
                    if refl is not None:
                        ent = str(refl.entryPoint)
                        if "PS" in usage: ps_entry = ent
                        else: cs_entry = ent
                except Exception:
                    pass
                consumer_eye.append({
                    "eventId": eid, "usage": usage, "viewport_x": vp_x,
                    "viewport_w": vp_w, "rt_width": rt_w, "eye": eye,
                    "psEntry": ps_entry, "csEntry": cs_entry,
                })
            except Exception:
                pass
        # Summarize
        from collections import Counter
        eye_counts = Counter(c["eye"] for c in consumer_eye)
        log(f"  eye distribution: {dict(eye_counts)}")
        for c in consumer_eye[:8]:
            entry = c.get("psEntry") or c.get("csEntry") or "?"
            log(f"    eid {c['eventId']} usage={c['usage'][:20]} vp_x={c['viewport_x']} eye={c['eye']} entry={entry}")
        results[resource] = {
            "label": label,
            "eyeDistribution": dict(eye_counts),
            "consumers": consumer_eye,
        }
    with open(os.path.join(OUT_DIR, "D_consumer_eyes.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    log("\nwrote D_consumer_eyes.json")

    # Verdict per subsystem
    log("\n=== VERDICTS PER SUBSYSTEM ===")
    for res, r in results.items():
        ed = r["eyeDistribution"]
        l = ed.get("left", 0) + ed.get("left_or_full", 0)
        rr = ed.get("right", 0)
        if rr == 0 and l > 0:
            verdict = "❗ ASYMMETRIC (left-only) — same bug pattern as water"
        elif rr > 0 and l > 0:
            verdict = "✓ symmetric (both eyes consume)"
        elif ed.get("compute_no_viewport", 0) > 0:
            verdict = "compute consumers (can't classify by viewport — check dispatch dim split)"
        else:
            verdict = "unknown"
        log(f"  {res} ({r['label']}): {verdict}  (eyes: {ed})")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
