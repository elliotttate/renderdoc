"""Per-eye descriptor-heap heatmap.

For every descriptor slot in every heap, record:
  - last_written_by_eye   (which eye's setup work last wrote into this slot)
  - last_consumed_by_eye  (which eye most recently sampled/bound this slot)
  - resource              (what's currently in the slot)

Mismatched rows (last_written != last_consumed) surface cross-eye
descriptor pollution at a glance — the exact pattern the SN2 right-eye
water-fog bug exhibits.

Output: JSON ``rows`` array, optionally written as a CSV for spreadsheet
inspection. Filterable by ``--mismatch-only``.

Usage::

    python -m util.automation.descriptor_heap_heatmap <cap.rdc> [--mismatch-only] [--csv heatmap.csv]
"""

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import eye_classifier  # type: ignore
else:
    from . import _lib, eye_classifier

import renderdoc as rd  # noqa: E402


def build_heatmap(capture_path: str) -> Dict[str, Any]:
    eye = eye_classifier.classify_capture(capture_path, {"mode": "auto"})
    eye_by_eid = {int(e["eventId"]): (e.get("eye") or "unknown") for e in eye.get("events", [])}

    cap, controller = _lib.open_capture(capture_path)
    try:
        # last_written: tracks the eye that most recently *bound* the slot
        # for a draw that *wrote* via UAV/RTV/DSV. For SRV-bound consumption
        # we track separately as last_consumed.
        # Map: (heap_str, slot_byte_offset) -> {eye, eventId, resource}
        last_written: Dict[Tuple[str, int], Dict[str, Any]] = {}
        last_consumed: Dict[Tuple[str, int], Dict[str, Any]] = {}

        for a in _lib.walk_actions(controller):
            flags = int(a.flags)
            if not (flags & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            eid = int(a.eventId)
            eye_label = eye_by_eid.get(eid, "unknown")
            try:
                controller.SetFrameEvent(eid, True)
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
                # Read-write (UAV) bindings — count as writers
                try:
                    arr_rw = pipe.GetReadWriteResources(stage_enum, False)
                except Exception:
                    arr_rw = []
                for used in arr_rw:
                    heap_str = _lib.resource_id_str(used.access.descriptorStore)
                    if heap_str is None:
                        continue
                    slot = int(used.access.byteOffset)
                    descriptor = used.descriptor
                    res = _lib.resource_id_str(descriptor.resource) if descriptor is not None else None
                    last_written[(heap_str, slot)] = {
                        "eye": eye_label,
                        "eventId": eid,
                        "resource": res,
                        "type": _lib.descriptor_type_name(used.access.type),
                    }
                # Read-only (SRV/CBV/Sampler) bindings — count as consumers
                for which in ("ReadOnly", "ConstantBlock", "Sampler"):
                    try:
                        if which == "ReadOnly":
                            arr = pipe.GetReadOnlyResources(stage_enum, False)
                        elif which == "ConstantBlock":
                            arr = pipe.GetConstantBlocks(stage_enum, False)
                        else:
                            arr = pipe.GetSamplers(stage_enum, False)
                    except Exception:
                        continue
                    for used in arr:
                        heap_str = _lib.resource_id_str(used.access.descriptorStore)
                        if heap_str is None:
                            continue
                        slot = int(used.access.byteOffset)
                        descriptor = used.descriptor
                        res = _lib.resource_id_str(descriptor.resource) if descriptor is not None else None
                        last_consumed[(heap_str, slot)] = {
                            "eye": eye_label,
                            "eventId": eid,
                            "resource": res,
                            "type": _lib.descriptor_type_name(used.access.type),
                        }

        # Merge the two maps
        keys = sorted(set(last_written.keys()) | set(last_consumed.keys()))
        rows = []
        mismatches = 0
        for k in keys:
            heap_str, slot = k
            lw = last_written.get(k)
            lc = last_consumed.get(k)
            row = {
                "heap": heap_str,
                "slot": slot,
                "lastWrittenByEye": (lw or {}).get("eye"),
                "lastWrittenEventId": (lw or {}).get("eventId"),
                "lastConsumedByEye": (lc or {}).get("eye"),
                "lastConsumedEventId": (lc or {}).get("eventId"),
                "lastWrittenResource": (lw or {}).get("resource"),
                "lastConsumedResource": (lc or {}).get("resource"),
                "type": (lc or lw or {}).get("type"),
            }
            mismatch = (
                row["lastWrittenByEye"] is not None
                and row["lastConsumedByEye"] is not None
                and row["lastWrittenByEye"] != row["lastConsumedByEye"]
                and row["lastWrittenByEye"] in ("left", "right")
                and row["lastConsumedByEye"] in ("left", "right")
            )
            row["mismatch"] = bool(mismatch)
            if mismatch:
                mismatches += 1
            rows.append(row)

        return {
            "summary": {
                "totalSlots": len(rows),
                "mismatches": mismatches,
                "heapsTouched": len(set(r["heap"] for r in rows)),
            },
            "rows": rows,
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Per-eye descriptor heap heatmap.")
    p.add_argument("capture")
    p.add_argument("--mismatch-only", action="store_true",
                   help="Only include slots where last-written and last-consumed eyes differ")
    p.add_argument("--csv", help="Optional CSV output path")
    p.add_argument("--out", "-o", help="JSON output path (default: stdout)")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = build_heatmap(args.capture)
    finally:
        rd.ShutdownReplay()

    if args.mismatch_only:
        out["rows"] = [r for r in out["rows"] if r.get("mismatch")]

    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            cols = ["heap", "slot", "lastWrittenByEye", "lastWrittenEventId", "lastConsumedByEye",
                    "lastConsumedEventId", "lastWrittenResource", "lastConsumedResource", "mismatch", "type"]
            w.writerow(cols)
            for row in out["rows"]:
                w.writerow([row.get(c) for c in cols])

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text[:5000] + ("..." if len(text) > 5000 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
