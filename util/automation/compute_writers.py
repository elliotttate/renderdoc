"""Compute-pipeline writer visibility.

For a given resource, find every dispatch that wrote to it and surface
the dispatch dimensions + UAV slot. Designed to immediately reveal
"X=0 workgroups" patterns (e.g., the SN2 right-eye fog volume bug where
view-1's Nanite dispatch ran with zero workgroups).

Combined with the eye_classifier, this produces side-by-side reports like:

    Resource A (164756):
      view-0 dispatch @ event 14502: X=70 Y=37 Z=128, UAV slot u0
      view-1 dispatch @ event 14601: X=0  Y=0  Z=0    <-- DEAD

Usage::

    python -m util.automation.compute_writers <capture.rdc> --resource ResourceId::1234
    python -m util.automation.compute_writers <capture.rdc> --resource <name> --eye-classify
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import eye_classifier  # type: ignore
else:
    from . import _lib
    from . import eye_classifier

import renderdoc as rd  # noqa: E402


def _is_writer(usage_str: str) -> bool:
    # RenderDoc's ResourceUsage names compute/pixel UAV writes as `CS_RWResource`
    # / `PS_RWResource`, not "CS_UAV". The original "UAV" filter missed every
    # GPU compute UAV write in modern D3D12/Vulkan captures. Match all known
    # PRODUCER usages by their actual RenderDoc names.
    #
    # Important: Discard/Barrier/Clear are deliberately EXCLUDED — they are
    # resource-lifecycle markers, not producers. A previous bug counted
    # `Discard` as a writer and caused the SN2 investigation to misidentify
    # `DiscardResource` events as the source of texture content; the actual
    # writers turned out to be the surrounding ColorTarget MRT draws.
    # `Clear` is a real producer of zeroed data so it stays.
    return any(k in usage_str for k in (
        "RWResource",          # CS_RWResource / PS_RWResource — UAV writes
        "RWBuffer",            # storage-buffer writes
        "UAV",                 # legacy fallback
        "ColourTarget",        # RTV — both UK/US spellings appear in different builds
        "ColorTarget",
        "DepthStencilTarget",  # DSV
        "CopyDst",             # CopyBufferRegion / CopyTextureRegion destination
        "ResolveDst",          # MSAA resolve destination
        "Resolve",             # variant
        "GenMips",             # automatic mip generation
        "Clear",               # ClearUnorderedAccessView / ClearRenderTargetView
        "StreamOut",           # transform feedback
    ))


def _is_lifecycle(usage_str: str) -> bool:
    """Return True for resource-lifecycle markers that are NOT real producers."""
    return any(k in usage_str for k in ("Discard", "Barrier"))


def _find_uav_slot_for_resource(controller, action, target_resource_id) -> Optional[Dict[str, Any]]:
    """For a compute dispatch action, look at the bound UAVs and find which slot
    holds ``target_resource_id``. Returns a dict with stage/register/space/heap
    or None if no UAV-slot match found.
    """
    pipe = controller.GetPipelineState()
    for stage_enum in (
        rd.ShaderStage.Compute,
        rd.ShaderStage.Pixel,
        rd.ShaderStage.Vertex,
    ):
        try:
            arr = pipe.GetReadWriteResources(stage_enum, False)
        except Exception:
            continue
        for used in arr:
            descriptor = used.descriptor
            if descriptor is None:
                continue
            rid = descriptor.resource
            if str(rid) == str(target_resource_id):
                return {
                    "stage": _lib.shader_stage_name(stage_enum),
                    "type": _lib.descriptor_type_name(used.access.type),
                    "byteOffset": int(used.access.byteOffset),
                    "byteSize": int(used.access.byteSize),
                    "heap": _lib.resource_id_str(used.access.descriptorStore),
                }
    return None


def find_writers(capture_path: str, resource_ref: str,
                 eye_classify: bool = False) -> Dict[str, Any]:
    """Build a per-event report for every action that wrote to ``resource_ref``.

    Each row includes:
      - eventId, eye (if classified)
      - action kind (Dispatch / DispatchIndirect / DrawXxx / Copy / Clear / Resolve)
      - dispatchDimension if it's a dispatch
      - dispatchThreadsDimension if it's a mesh-style dispatch
      - bound UAV slot (stage, register, space, heap, byteOffset) if findable
      - usage string from GetUsage

    The "X=0 workgroups" pattern shows up as an explicit row with
    ``dispatchDimension == [0, 0, 0]``.
    """
    cap, controller = _lib.open_capture(capture_path)
    try:
        # Resolve ref to a ResourceId.
        target = None
        for r in controller.GetResources():
            if str(r.resourceId) == resource_ref or str(r.name) == resource_ref:
                target = r.resourceId
                break
        if target is None:
            return {"error": f"resource not found: {resource_ref}"}

        usage = controller.GetUsage(target)
        writers = [u for u in usage if _is_writer(str(u.usage).split(".")[-1])]
        eye_index: Dict[int, str] = {}
        if eye_classify:
            ec = eye_classifier.classify_capture(capture_path, {"mode": "auto"})
            for e in ec.get("events", []):
                eye_index[int(e["eventId"])] = e.get("eye") or "unknown"

        # Build action lookup
        action_by_eid: Dict[int, Any] = {}
        for a in _lib.walk_actions(controller):
            action_by_eid[int(a.eventId)] = a

        rows: List[Dict[str, Any]] = []
        dead_dispatches = 0
        for w in writers:
            eid = int(w.eventId)
            action = action_by_eid.get(eid)
            row: Dict[str, Any] = {
                "eventId": eid,
                "usage": str(w.usage).split(".")[-1],
                "eye": eye_index.get(eid, "unknown") if eye_classify else None,
            }
            if action is not None:
                row["name"] = str(action.GetName(controller.GetStructuredFile()))
                flags = int(action.flags)
                row["flags"] = _lib.action_flag_names(flags)
                if flags & int(rd.ActionFlags.Dispatch):
                    dd = list(action.dispatchDimension)
                    dt = list(action.dispatchThreadsDimension)
                    row["dispatchDimension"] = [int(v) for v in dd]
                    if any(int(v) != 0 for v in dt):
                        row["dispatchThreadsDimension"] = [int(v) for v in dt]
                    if all(int(v) == 0 for v in dd):
                        row["dead"] = True
                        dead_dispatches += 1
                if flags & int(rd.ActionFlags.Drawcall):
                    row["numIndices"] = int(action.numIndices)
                    row["numInstances"] = int(action.numInstances)

                try:
                    controller.SetFrameEvent(eid, True)
                    uav = _find_uav_slot_for_resource(controller, action, target)
                    if uav is not None:
                        row["uav"] = uav
                except Exception as exc:
                    row["uavError"] = str(exc)
            rows.append(row)

        summary = {
            "resource": str(target),
            "writeCount": len(rows),
            "deadDispatches": dead_dispatches,
        }
        if eye_classify:
            per_eye: Dict[str, int] = {}
            dead_per_eye: Dict[str, int] = {}
            for r in rows:
                e = r.get("eye") or "unknown"
                per_eye[e] = per_eye.get(e, 0) + 1
                if r.get("dead"):
                    dead_per_eye[e] = dead_per_eye.get(e, 0) + 1
            summary["perEyeWrites"] = per_eye
            summary["deadPerEye"] = dead_per_eye

        return {"summary": summary, "writes": rows}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def find_writers_by_eye(capture_path: str, resource_ref: str) -> Dict[str, Any]:
    """Convenience wrapper for the side-by-side L/R writer report."""
    return find_writers(capture_path, resource_ref, eye_classify=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Show every dispatch/draw that wrote to a resource, with workgroup counts.")
    p.add_argument("capture")
    p.add_argument("--resource", required=True, help="Resource ID (e.g. ResourceId::164756) or resource name")
    p.add_argument("--eye-classify", action="store_true",
                   help="Also classify each writer event as left/right eye")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = find_writers(args.capture, args.resource, eye_classify=args.eye_classify)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
