"""Resource barrier / state-transition history.

For each event of interest, dump the D3D12 resource state for every live
resource (D3D12Pipe::State.resourceStates). Useful for confirming whether
a UAV barrier or transition happened correctly between left- and right-eye
work that shares a resource.

Also tags actions whose `flags` include Barrier (RenderDoc surfaces them as
their own actions in some captures).

Usage:
    python -m util.automation.barrier_history <cap.rdc> [--for-resource <ref>] [--out file]
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _resource_state_at_event(controller, eid: int, for_resource_id=None) -> list:
    controller.SetFrameEvent(int(eid), True)
    try:
        d3d12 = controller.GetD3D12PipelineState()
    except Exception:
        return []
    if d3d12 is None:
        return []
    rows = []
    for rd_state in d3d12.resourceStates:
        rid = _lib.resource_id_str(rd_state.resourceId)
        if for_resource_id is not None and rid != for_resource_id:
            continue
        states = []
        for s in rd_state.states:
            states.append({
                "name": str(s.name) if hasattr(s, "name") else None,
                "firstMip": int(s.firstMip) if hasattr(s, "firstMip") else None,
                "firstSlice": int(s.firstSlice) if hasattr(s, "firstSlice") else None,
            })
        rows.append({"resource": rid, "states": states})
    return rows


def history(capture: str, for_resource=None) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        # Resolve for_resource to a ResourceId string
        target = None
        if for_resource:
            for r in controller.GetResources():
                if str(r.resourceId) == for_resource or str(r.name) == for_resource:
                    target = str(r.resourceId)
                    break
            if target is None:
                return {"error": f"resource not found: {for_resource}"}

        prev_signature = {}
        timeline = []
        for a in _lib.walk_actions(controller):
            if not (int(a.flags) & (
                int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch) |
                int(rd.ActionFlags.Copy) | int(rd.ActionFlags.Resolve) |
                int(rd.ActionFlags.Clear) | int(rd.ActionFlags.GenMips)
            )):
                continue
            eid = int(a.eventId)
            row = _resource_state_at_event(controller, eid, target)
            for r in row:
                rid = r["resource"]
                sig = tuple(sorted((s.get("name"), s.get("firstMip"), s.get("firstSlice")) for s in r["states"]))
                if prev_signature.get(rid) != sig:
                    timeline.append({
                        "eventId": eid,
                        "resource": rid,
                        "states": r["states"],
                    })
                    prev_signature[rid] = sig
        return {"resource": target, "transitions": timeline}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Resource barrier / state transition history.")
    p.add_argument("capture")
    p.add_argument("--for-resource", help="Only report transitions for this resource")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = history(args.capture, args.for_resource)
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
