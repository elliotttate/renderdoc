"""Priority 1: output-RT enumeration for LEFT-only PS shaders.

Process the 4 priority candidates (InjectMainPS x 2, SubsurfaceRecombine*PS x 2)
FIRST, then iterate the remaining 22. For each:
  - Output RTs (resource uid, format, dim, slot)
  - Per-eye readers (event#, PS_CRC, SRV slot)
  - Per-eye-allocated vs shared verdict

Designed to be memory-light: limit each shader's reader-classification to
at most 20 reader events (cap to avoid full-capture SetFrameEvent walk).
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "left_only_outputs_v2.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"
inv = json.load(open(os.path.join(OUT_DIR, "ps_crc_inventory.json")))
main_w, main_h = inv["main_rt"]
half_w = main_w / 2

PRIORITY_HASHES = [
    "e8f09d3c49cccfae",  # InjectMainPS
    "fd73062da2727b98",  # InjectMainPS
    "9c2134cc5ed6c2e1",  # SubsurfaceRecombineCopyPS
    "f8faccf002f99091",  # SubsurfaceRecombinePS
]

left_only_keys = inv.get("left_only", [])
left_events = inv.get("left", {})

# Order: priority candidates first
ordered_keys = []
for h in PRIORITY_HASHES:
    for k in left_only_keys:
        if k.startswith(h):
            ordered_keys.append(k)
for k in left_only_keys:
    if k not in ordered_keys:
        ordered_keys.append(k)
log(f"  {len(ordered_keys)} LEFT-only PS shaders to investigate "
    f"({len(PRIORITY_HASHES)} priority candidates first)")


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def classify_eye(d3d12, half_threshold):
    """Eye via viewport.x."""
    try:
        if len(d3d12.rasterizer.viewports) > 0:
            vp_x = float(d3d12.rasterizer.viewports[0].x)
            if vp_x >= half_threshold * 0.5:
                return "right", vp_x
            else:
                return "left", vp_x
    except Exception:
        pass
    return "unknown", None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # Collect outputs for each LEFT-only shader
    log("\n=== Per-shader output + consumer analysis ===")
    report = []
    for key in ordered_keys:
        h, entry = key.split("|", 1)
        eids = left_events.get(key, [])
        if not eids:
            continue
        producer_eid = eids[0]
        log(f"\n--- {h[:16]} ({entry}) — first event {producer_eid} ---")
        try:
            controller.SetFrameEvent(producer_eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()
        except Exception as e:
            log(f"    SetFrameEvent failed: {e}")
            continue
        # Outputs: RTVs + UAVs
        outputs = []
        try:
            for i, rt in enumerate(d3d12.outputMerger.renderTargets):
                rid = res_id(rt.resource)
                if not rid:
                    continue
                tex = textures_by_id.get(rid)
                fmt = str(tex.format.Name()) if tex else "?"
                dim_str = f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)}" if tex else "?"
                outputs.append({
                    "role": "RTV", "slot": i, "resource": rid,
                    "format": fmt, "dim": dim_str,
                    "type": str(tex.type).split(".")[-1] if tex else None,
                })
        except Exception:
            pass
        try:
            for u in pipe.GetReadWriteResources(rd.ShaderStage.Pixel, False):
                d = u.descriptor
                if d is None or d.resource is None:
                    continue
                rid = res_id(d.resource)
                tex = textures_by_id.get(rid)
                fmt = str(tex.format.Name()) if tex else "?"
                dim_str = f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)}" if tex else "?"
                outputs.append({
                    "role": "PS_UAV", "slot": int(u.access.index), "resource": rid,
                    "format": fmt, "dim": dim_str,
                    "type": str(tex.type).split(".")[-1] if tex else None,
                })
        except Exception:
            pass
        log(f"    outputs ({len(outputs)}):")
        for o in outputs:
            log(f"      {o['role']} slot {o['slot']}: {o['resource']}  [{o['dim']} {o['format']}  {o.get('type')}]")

        # For each output, find readers + classify eye
        per_output = []
        for o in outputs:
            rid_str = o["resource"]
            target = None
            for r in controller.GetResources():
                if res_id(r.resourceId) == rid_str:
                    target = r.resourceId; break
            if target is None:
                continue
            usage = controller.GetUsage(target)
            readers = []
            for u in usage:
                kind = str(u.usage).split(".")[-1]
                if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind:
                    continue
                if "Discard" in kind or "Barrier" in kind:
                    continue
                if "Resource" in kind:
                    readers.append({"eventId": int(u.eventId), "usage": kind})
            # Classify each reader by eye (cap at 30 to keep runtime low)
            per_eye_readers = {"left": [], "right": [], "unknown": []}
            for r in readers[:30]:
                eid = r["eventId"]
                try:
                    controller.SetFrameEvent(eid, True)
                    d3d12_r = controller.GetD3D12PipelineState()
                    pipe_r = controller.GetPipelineState()
                except Exception:
                    continue
                eye, vp_x = classify_eye(d3d12_r, half_w)
                # Which SRV slot binds this resource at this reader?
                srv_slot = None
                try:
                    for u in pipe_r.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
                        d = u.descriptor
                        if d and d.resource and res_id(d.resource) == rid_str:
                            srv_slot = int(u.access.index)
                            break
                except Exception:
                    pass
                # Reader PS hash
                ps_hash = None
                ps_entry = None
                try:
                    refl = pipe_r.GetShaderReflection(rd.ShaderStage.Pixel)
                    if refl and len(refl.rawBytes) > 0:
                        ps_hash = _lib.shader_bytecode_hash(bytes(refl.rawBytes))[:16]
                        ps_entry = str(refl.entryPoint)
                except Exception:
                    pass
                per_eye_readers[eye].append({
                    "eventId": eid, "usage": r["usage"],
                    "viewport_x": vp_x, "srvSlot": srv_slot,
                    "psHash": ps_hash, "psEntry": ps_entry,
                })
            per_output.append({"output": o, "perEye": per_eye_readers,
                                "totalReaders": len(readers)})

            log(f"    output {rid_str} consumers:  L={len(per_eye_readers['left'])} "
                f"R={len(per_eye_readers['right'])} U={len(per_eye_readers['unknown'])} "
                f"(total readers in capture: {len(readers)})")
            for er in per_eye_readers["right"][:5]:
                log(f"      RIGHT eid {er['eventId']} ({er['psEntry']}) t{er['srvSlot']}")
            for er in per_eye_readers["left"][:3]:
                log(f"      LEFT  eid {er['eventId']} ({er['psEntry']}) t{er['srvSlot']}")

        # Per-eye-allocated vs shared verdict per output
        for po in per_output:
            o = po["output"]
            # Check if there's a "right-eye equivalent" output (another resource of
            # same shape/format written by ANY right-eye event)
            same_shape_count = 0
            for trid, tex in textures_by_id.items():
                if not tex:
                    continue
                if str(tex.format.Name()) == o["format"] and \
                   f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)}" == o["dim"]:
                    same_shape_count += 1
            o["same_shape_resources_in_capture"] = same_shape_count

        report.append({
            "key": key, "hash": h, "entry": entry,
            "producer_event": producer_eid,
            "outputs": per_output,
        })

    out_path = os.path.join(OUT_DIR, "left_only_outputs_v2.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log(f"\nwrote {out_path}")

    # Summary verdict
    log("\n=== KEY CHAIN SUMMARY ===")
    for r in report:
        if not r["outputs"]:
            continue
        # Per-output, surface RIGHT readers
        for po in r["outputs"]:
            right = po["perEye"]["right"]
            if right:
                o = po["output"]
                log(f"  ⚠ {r['hash'][:16]} ({r['entry']}) writes {o['role']}{o['slot']} "
                    f"= {o['resource']} [{o['dim']} {o['format']}]")
                log(f"     → {len(right)} RIGHT-eye reader(s); same-shape resources in capture: "
                    f"{o.get('same_shape_resources_in_capture', '?')}")
                for er in right[:3]:
                    log(f"       eid {er['eventId']} t{er['srvSlot']} {er['psEntry']}")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
