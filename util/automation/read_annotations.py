"""Read RENDERDOC_SetCommandAnnotation / SetObjectAnnotation values from a capture.

The annotations API has existed in RenderDoc since v1.7.0 but the docs
for using it from a UEVR-style mid-hook plugin are sparse. This module
demonstrates the reader side: given a capture, dump every annotation
attached to any event or resource.

The writer side from UEVR-style code looks like::

    // Set an annotation on the active command list before issuing a draw.
    RENDERDOC_API_1_7_0 *rdoc = /* obtained via RENDERDOC_GetAPI */;
    RENDERDOC_AnnotationValue v;
    v.string = "right_eye";
    rdoc->SetCommandAnnotation(device, cmdList, "uevr.eye",
                               eRENDERDOC_String, 0, &v);

Then this script surfaces ``("right_eye", on event 16042)`` in the
``event_annotations`` section.

Usage::

    python -m util.automation.read_annotations <cap.rdc> [--out file]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _sdobject_to_dict(obj, max_depth: int = 4) -> Any:
    if obj is None or max_depth <= 0:
        return None
    bt = str(obj.type.basetype).split(".")[-1] if hasattr(obj, "type") else ""
    try:
        if bt == "String":
            return str(obj.AsString())
        if bt == "Float":
            return float(obj.AsFloat())
        if bt == "SignedInteger" or bt == "UnsignedInteger":
            return int(obj.AsInt())
        if bt == "Resource":
            return str(obj.AsResourceId())
    except Exception:
        pass
    if obj.NumChildren() > 0:
        out: Dict[str, Any] = {}
        for i in range(obj.NumChildren()):
            c = obj.GetChild(i)
            out[str(c.name)] = _sdobject_to_dict(c, max_depth - 1)
        return out
    return None


def dump(capture_path: str) -> Dict[str, Any]:
    cap, controller = _lib.open_capture(capture_path)
    try:
        resource_annotations: List[Dict[str, Any]] = []
        for r in controller.GetResources():
            ann = r.annotations
            if ann is None:
                continue
            resource_annotations.append({
                "resourceId": str(r.resourceId),
                "name": str(r.name),
                "annotations": _sdobject_to_dict(ann),
            })

        event_annotations: List[Dict[str, Any]] = []
        for a in _lib.walk_actions(controller):
            for ev in a.events:
                ann = getattr(ev, "annotations", None)
                if ann is None:
                    continue
                event_annotations.append({
                    "eventId": int(ev.eventId),
                    "annotations": _sdobject_to_dict(ann),
                })

        return {
            "summary": {
                "resourcesWithAnnotations": len(resource_annotations),
                "eventsWithAnnotations": len(event_annotations),
            },
            "resourceAnnotations": resource_annotations,
            "eventAnnotations": event_annotations,
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Dump every RENDERDOC_*Annotation in a capture.")
    p.add_argument("capture")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = dump(args.capture)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text[:8000] + ("..." if len(text) > 8000 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
