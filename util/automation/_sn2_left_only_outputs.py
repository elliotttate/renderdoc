"""Filter the 26 LEFT-only PS shaders to find the one(s) whose output
gets consumed by RIGHT-eye downstream passes.

For each LEFT-only PS shader:
  1. Find one event using it
  2. Read the bound RT(s) at output merger + any UAVs
  3. For each output resource: find all PS/VS/CS readers, classify by eye
  4. Surface LEFT-only-producer → RIGHT-eye-reader chains as the patch
     targets
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "left_only_outputs.log")


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

# Take the LEFT-only PS list — these are key=H|entry, value=list of event IDs
left_only_keys = inv.get("left_only", [])
left_events = inv.get("left", {})
log(f"  loaded {len(left_only_keys)} LEFT-only PS shaders to investigate")


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # For each LEFT-only shader, get RT outputs at one event
    log("\n=== Phase 1: collect output resources for each LEFT-only shader ===")
    left_only_outputs = {}   # key -> list of {resource, role(RTV/UAV)}
    for key in left_only_keys:
        eids = left_events.get(key, [])
        if not eids:
            continue
        eid = eids[0]
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()
        except Exception:
            continue
        rts = []
        try:
            for i, rt in enumerate(d3d12.outputMerger.renderTargets):
                rid = res_id(rt.resource)
                if rid:
                    rts.append({"slot": i, "resource": rid, "role": "RTV"})
        except Exception:
            pass
        # PS UAVs
        try:
            for u in pipe.GetReadWriteResources(rd.ShaderStage.Pixel, False):
                d = u.descriptor
                if d and d.resource:
                    rts.append({"slot": int(u.access.index),
                                "resource": res_id(d.resource), "role": "PS_UAV"})
        except Exception:
            pass
        left_only_outputs[key] = {"firstEvent": eid, "outputs": rts}
        h, entry = key.split("|", 1)
        log(f"  {h[:16]} ({entry[:30]:30s}) @ eid {eid}: "
            f"{len(rts)} outputs — {[(r['role'], r['resource']) for r in rts[:3]]}")

    # Aggregate all unique output resources
    all_outputs = set()
    for v in left_only_outputs.values():
        for o in v["outputs"]:
            all_outputs.add(o["resource"])
    log(f"\n  {len(all_outputs)} unique output resources from LEFT-only PSes")

    # ----- Phase 2: for each unique output resource, find right-eye readers -----
    log("\n=== Phase 2: trace consumers per output resource ===")
    consumers = {}
    for rid_str in sorted(all_outputs):
        target = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == rid_str:
                target = r.resourceId; break
        if target is None:
            continue
        # Get all readers (PS/VS/CS) and classify by eye via viewport
        usage = controller.GetUsage(target)
        reader_events = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            # Real readers — not writers and not lifecycle markers
            if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind:
                continue
            if "Discard" in kind or "Barrier" in kind:
                continue
            if "Resource" not in kind:
                continue
            reader_events.append({"eventId": int(u.eventId), "usage": kind})

        if not reader_events:
            continue
        # Classify each reader by eye
        # (use viewport.x at the event; events at vp_x >= half_w are RIGHT)
        per_eye_readers = {"left": [], "right": [], "unknown": []}
        for r in reader_events[:50]:  # cap at 50 to save time
            try:
                controller.SetFrameEvent(r["eventId"], True)
                d3d12 = controller.GetD3D12PipelineState()
                vp_x = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp_x = float(d3d12.rasterizer.viewports[0].x)
                if vp_x is None:
                    eye = "unknown"
                elif vp_x >= half_w * 0.5:
                    eye = "right"
                else:
                    eye = "left"
                # Get shader entry
                pipe = controller.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                per_eye_readers[eye].append({"eventId": r["eventId"], "usage": r["usage"],
                                               "viewport_x": vp_x, "psEntry": entry})
            except Exception:
                pass
        info = textures_by_id.get(rid_str)
        consumers[rid_str] = {
            "info": {"w": int(info.width), "h": int(info.height), "d": int(info.depth),
                     "format": str(info.format.Name())} if info else None,
            "perEyeReaders": per_eye_readers,
            "totalReaders": len(reader_events),
        }

    # ----- Surface key chains -----
    log("\n=== KEY CHAINS: LEFT-only producer → RIGHT-eye reader ===")
    chains = []
    for key, prod_info in left_only_outputs.items():
        h, entry = key.split("|", 1)
        for out in prod_info["outputs"]:
            rid = out["resource"]
            cons = consumers.get(rid, {})
            right_readers = cons.get("perEyeReaders", {}).get("right", [])
            if right_readers:
                chain = {
                    "producer_ps": h[:16],
                    "producer_entry": entry,
                    "producer_event": prod_info["firstEvent"],
                    "output_resource": rid,
                    "output_info": cons.get("info"),
                    "right_reader_count": len(right_readers),
                    "right_readers_sample": right_readers[:5],
                }
                chains.append(chain)
                log(f"\n  ⚠ LEFT-only producer {h[:16]} ({entry}) @ eid {prod_info['firstEvent']}")
                log(f"      → writes {rid} ({cons.get('info')})")
                log(f"      → READ by {len(right_readers)} RIGHT-eye events:")
                for r in right_readers[:5]:
                    log(f"          eid {r['eventId']} (PS={r['psEntry']}, vp_x={r['viewport_x']})")
    log(f"\n  {len(chains)} LEFT-only-producer → RIGHT-eye-reader chains found")

    out = {
        "leftOnlyOutputs": left_only_outputs,
        "consumers": consumers,
        "chains": chains,
    }
    with open(os.path.join(OUT_DIR, "left_only_outputs.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nwrote left_only_outputs.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
