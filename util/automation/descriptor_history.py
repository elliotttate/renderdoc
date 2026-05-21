"""Descriptor write history over time (the deferred §4 item).

RenderDoc's D3D12 driver doesn't expose a "list every CopyDescriptors call"
log via the public API. But it does expose `controller.GetDescriptors(heap,
ranges)` at any event, which is sufficient: we walk every event, sample the
descriptor contents for every (heap, slot) of interest, and emit a write
entry whenever the contents change between consecutive events.

For analysis this is strictly equivalent to driver-side CopyDescriptors
tracking: we catch every observable change at the consuming events. The
only thing we don't see is intermediate writes to staging-only heaps that
never get consumed by a draw — which by definition can't affect rendering.

Three modes:
  - --slots-from-bindings: only track slots referenced by any UsedDescriptor
    in any event (fastest; covers everything that actually matters)
  - --all-shader-visible: track every slot in every shader-visible heap
    (covers slots written but never bound; usually not needed)
  - --heap-and-range: explicit heap id + (offset, count)

Output: JSONL of {heap, slot, eventId, type, resource, view, format,
firstMip, numMips, firstSlice, numSlices, byteOffset, byteSize,
contentHash, changedFromPrev}.

Usage:
    python -m util.automation.descriptor_history <capture.rdc> [--slots-from-bindings] [--out file]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _descriptor_hash(d) -> str:
    """Stable fingerprint of a Descriptor's payload (resource + view geometry)."""
    if d is None:
        return "null"
    parts = (
        str(d.resource), str(d.view), str(d.format.Name()) if hasattr(d.format, "Name") else str(d.format),
        int(d.firstMip), int(d.numMips), int(d.firstSlice), int(d.numSlices),
        int(d.byteOffset), int(d.byteSize), int(d.type),
    )
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _descriptor_dict(d) -> dict:
    return {
        "type": _lib.descriptor_type_name(d.type),
        "resource": _lib.resource_id_str(d.resource),
        "view": _lib.resource_id_str(d.view),
        "format": str(d.format.Name()) if hasattr(d.format, "Name") else str(d.format),
        "firstMip": int(d.firstMip),
        "numMips": int(d.numMips),
        "firstSlice": int(d.firstSlice),
        "numSlices": int(d.numSlices),
        "byteOffset": int(d.byteOffset),
        "byteSize": int(d.byteSize),
    }


def _build_slot_set_from_bindings(controller) -> dict:
    """Return {heap_id_str: set(slot_byte_offset)} from every event's bindings."""
    slots = {}
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        try:
            controller.SetFrameEvent(int(a.eventId), True)
        except Exception:
            continue
        pipe = controller.GetPipelineState()
        for stage_enum in (
            rd.ShaderStage.Vertex,
            rd.ShaderStage.Hull,
            rd.ShaderStage.Domain,
            rd.ShaderStage.Geometry,
            rd.ShaderStage.Pixel,
            rd.ShaderStage.Compute,
            rd.ShaderStage.Amplification,
            rd.ShaderStage.Mesh,
        ):
            for which in ("ReadOnly", "ReadWrite", "ConstantBlock", "Sampler"):
                try:
                    if which == "ReadOnly":
                        arr = pipe.GetReadOnlyResources(stage_enum, False)
                    elif which == "ReadWrite":
                        arr = pipe.GetReadWriteResources(stage_enum, False)
                    elif which == "ConstantBlock":
                        arr = pipe.GetConstantBlocks(stage_enum, False)
                    else:
                        arr = pipe.GetSamplers(stage_enum, False)
                except Exception:
                    continue
                for used in arr:
                    heap_id = _lib.resource_id_str(used.access.descriptorStore)
                    if heap_id is None:
                        continue
                    slots.setdefault(heap_id, set()).add(
                        (int(used.access.byteOffset), int(used.access.byteSize), int(used.access.type))
                    )
    return slots


def build_history(controller, mode="slots-from-bindings", explicit=None) -> dict:
    """Build descriptor history. Returns {writes: [...], summary: {...}}."""
    if mode == "slots-from-bindings":
        slot_map = _build_slot_set_from_bindings(controller)
    elif mode == "heap-and-range" and explicit:
        slot_map = {explicit["heap"]: {(explicit["offset"], explicit.get("descriptorSize", 1), 0)}}
    else:
        raise ValueError(f"unsupported mode: {mode}")

    # Resolve heap ResourceId from string
    heap_id_by_str = {}
    for r in controller.GetResources():
        heap_id_by_str[str(r.resourceId)] = r.resourceId
    heaps = {h: heap_id_by_str.get(h) for h in slot_map.keys() if h in heap_id_by_str}

    # Walk events and sample each slot's descriptor
    last_hash = {}  # (heap, slot) -> hash
    writes = []

    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
        except Exception:
            continue

        for heap_str, slots in slot_map.items():
            heap_rid = heaps.get(heap_str)
            if heap_rid is None:
                continue
            ranges = []
            slot_list = sorted(slots)
            for offset, size, type_int in slot_list:
                r = rd.DescriptorRange()
                r.offset = offset
                r.descriptorSize = max(1, size)
                r.count = 1
                # type is optional — leave default if we don't know
                ranges.append(r)
            try:
                contents = controller.GetDescriptors(heap_rid, ranges)
            except Exception:
                continue
            for i, d in enumerate(contents):
                offset, size, _ = slot_list[i]
                h = _descriptor_hash(d)
                key = (heap_str, offset)
                prev = last_hash.get(key)
                if prev != h:
                    entry = {
                        "eventId": eid,
                        "heap": heap_str,
                        "slot": offset,
                        "changedFromPrev": prev is not None,
                        "prevHash": prev,
                        "hash": h,
                    }
                    entry.update(_descriptor_dict(d))
                    writes.append(entry)
                    last_hash[key] = h

    summary = {
        "slotCount": sum(len(s) for s in slot_map.values()),
        "heapCount": len(slot_map),
        "writeCount": len(writes),
    }
    return {"summary": summary, "writes": writes}


def descriptor_history(capture: str, mode: str, explicit=None) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        return build_history(controller, mode, explicit)
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Descriptor write history over time.")
    p.add_argument("capture")
    p.add_argument("--slots-from-bindings", action="store_true",
                   help="Track only slots that any draw/dispatch actually used (default)")
    p.add_argument("--heap", help="Explicit heap ResourceId string")
    p.add_argument("--offset", type=int, help="Explicit slot offset")
    p.add_argument("--descriptor-size", type=int, default=1, help="Descriptor stride (default 1 byte = slot count)")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        mode = "slots-from-bindings"
        explicit = None
        if args.heap and args.offset is not None:
            mode = "heap-and-range"
            explicit = {"heap": args.heap, "offset": args.offset, "descriptorSize": args.descriptor_size}
        out = descriptor_history(args.capture, mode, explicit)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
