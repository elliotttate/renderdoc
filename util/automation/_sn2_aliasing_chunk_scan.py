"""Pure-CPU SDFile walk to find ALL aliasing barriers in the capture
and correlate them to LightScatteringCS / UWE Fog dispatch event ranges.

No SetFrameEvent calls — only structured chunk traversal. Should run
in under a minute even under memory pressure.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_aliasing_verify"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "aliasing_chunk_scan.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"


def res_id_from_sdobj(obj):
    """Pull a ResourceId from an SDObject."""
    try:
        return _lib.resource_id_str(obj.AsResourceId())
    except Exception:
        pass
    try:
        return str(obj.data.basic.resourceId)
    except Exception:
        pass
    return None


def find_child(obj, name):
    """Find first child by name."""
    try:
        for i in range(obj.NumChildren()):
            c = obj.GetChild(i)
            if str(c.name) == name:
                return c
    except Exception:
        pass
    return None


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    log(f"sdfile has {nchunks} chunks")

    # Pass 1: find all ResourceBarrier chunks + extract aliasing barriers
    aliasing_barriers = []   # list of {chunkIndex, eid, resourceBefore, resourceAfter}
    transition_count = 0
    uav_barrier_count = 0
    aliasing_count = 0

    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "ResourceBarrier" not in cname:
            continue
        eid = None
        try:
            eid = int(chunk.metadata.eventId)
        except Exception:
            pass
        # Locate the barriers array
        barriers_arr = find_child(chunk, "pBarriers") or find_child(chunk, "Barriers")
        if barriers_arr is None:
            continue
        try:
            nbarriers = barriers_arr.NumChildren()
        except Exception:
            continue
        for k in range(nbarriers):
            b = barriers_arr.GetChild(k)
            type_child = find_child(b, "Type")
            if type_child is None:
                continue
            try:
                tv = int(type_child.AsInt())
            except Exception:
                continue
            if tv == 0:
                transition_count += 1
            elif tv == 1:
                # ALIASING
                aliasing_count += 1
                # Find Aliasing sub-struct (union member)
                aliasing_child = find_child(b, "Aliasing")
                before_id = None
                after_id = None
                if aliasing_child is not None:
                    rb = find_child(aliasing_child, "pResourceBefore")
                    ra = find_child(aliasing_child, "pResourceAfter")
                    if rb is not None:
                        before_id = res_id_from_sdobj(rb)
                    if ra is not None:
                        after_id = res_id_from_sdobj(ra)
                aliasing_barriers.append({
                    "chunkIndex": i, "eid": eid,
                    "before": before_id, "after": after_id,
                })
            elif tv == 2:
                uav_barrier_count += 1

    log(f"\n=== BARRIER SUMMARY ===")
    log(f"  total transitions: {transition_count}")
    log(f"  total aliasing barriers: {aliasing_count}")
    log(f"  total UAV barriers: {uav_barrier_count}")

    if aliasing_count == 0:
        log(f"\n→ NO aliasing barriers in this capture. The colleague's placed-heap")
        log(f"  aliasing hypothesis does NOT apply to this capture. The bug here is")
        log(f"  Shape A: right-eye dispatches run with X=0 + no UAVs (deliberate no-op).")
    else:
        log(f"\n=== ALIASING DETAIL ===")
        # Histogram by resource pairs
        from collections import Counter
        pair_counts = Counter()
        for ab in aliasing_barriers:
            pair_counts[(ab["before"], ab["after"])] += 1
        log(f"  unique (before, after) pairs: {len(pair_counts)}")
        for (before, after), cnt in pair_counts.most_common(20):
            log(f"    {before} → {after}: {cnt} barriers")

        # Find aliasing barriers near known UWE Fog / LightScattering events
        log(f"\n=== ALIASING BARRIERS NEAR FOG DISPATCH EVENTS ===")
        FOG_EVENTS = [20206, 21348, 21359, 21370, 21377, 21390, 24726, 24743]
        for fe in FOG_EVENTS:
            near = [ab for ab in aliasing_barriers
                    if ab["eid"] is not None and abs(int(ab["eid"]) - fe) <= 20]
            if near:
                log(f"  near event {fe} (within ±20):")
                for ab in near[:6]:
                    log(f"    eid {ab['eid']} (chunk #{ab['chunkIndex']}): "
                        f"{ab['before']} → {ab['after']}")

    # Pass 2: find chunks that bind/Dispatch in the fog-related event range,
    # to characterise the chunk neighborhood.
    log(f"\n=== Pass 2: chunk types near fog events ===")
    FOG_EVENT_WINDOW = (20100, 21500)
    chunk_kinds_in_window = {}
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        try:
            eid = int(chunk.metadata.eventId)
        except Exception:
            continue
        if FOG_EVENT_WINDOW[0] <= eid <= FOG_EVENT_WINDOW[1]:
            cname = str(chunk.name)
            chunk_kinds_in_window[cname] = chunk_kinds_in_window.get(cname, 0) + 1
    log(f"  chunk kinds in [{FOG_EVENT_WINDOW[0]}, {FOG_EVENT_WINDOW[1]}]:")
    for k, v in sorted(chunk_kinds_in_window.items(), key=lambda kv: -kv[1])[:20]:
        log(f"    {k}: {v}")

    out = {
        "barrierSummary": {
            "transition": transition_count,
            "aliasing": aliasing_count,
            "uav": uav_barrier_count,
        },
        "aliasingBarriers": aliasing_barriers[:200],
    }
    with open(os.path.join(OUT_DIR, "aliasing_chunk_scan.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nwrote aliasing_chunk_scan.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
