"""PostVS / mesh-stage data per event — diff geometry between eyes.

Wraps controller.GetPostVSData(instance, view, stage) to extract the
transformed vertex stream emitted by each stage (VS, GS, DS, mesh) at a
given event, and (optionally) diffs two events to surface geometry that
differs between eyes.

Usage:
    python -m util.automation.geometry_diff dump <cap.rdc> --event 16042 --stage VSOut
    python -m util.automation.geometry_diff compare <cap.rdc> --left 16042 --right 16678 --stage VSOut
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


_STAGE_ENUM = {
    "VSOut": rd.MeshDataStage.VSOut if hasattr(rd, "MeshDataStage") else None,
    "GSOut": getattr(rd.MeshDataStage, "GSOut", None) if hasattr(rd, "MeshDataStage") else None,
    "DSOut": getattr(rd.MeshDataStage, "DSOut", None) if hasattr(rd, "MeshDataStage") else None,
    "TaskOut": getattr(rd.MeshDataStage, "TaskOut", None) if hasattr(rd, "MeshDataStage") else None,
    "MeshOut": getattr(rd.MeshDataStage, "MeshOut", None) if hasattr(rd, "MeshDataStage") else None,
    "Count": None,
}


def _meshformat_summary(controller, mf):
    out = {
        "numIndices": int(mf.numIndices) if hasattr(mf, "numIndices") else None,
        "indexByteOffset": int(mf.indexByteOffset) if hasattr(mf, "indexByteOffset") else None,
        "vertexByteOffset": int(mf.vertexByteOffset) if hasattr(mf, "vertexByteOffset") else None,
        "vertexByteStride": int(mf.vertexByteStride) if hasattr(mf, "vertexByteStride") else None,
        "vertexResourceId": _lib.resource_id_str(mf.vertexResourceId) if hasattr(mf, "vertexResourceId") else None,
        "indexResourceId": _lib.resource_id_str(mf.indexResourceId) if hasattr(mf, "indexResourceId") else None,
        "topology": str(mf.topology).split(".")[-1] if hasattr(mf, "topology") else None,
        "numVertices": None,
    }
    # Cheap: try to dump first 24 floats from the vertex buffer for a fingerprint
    try:
        if hasattr(mf, "vertexResourceId") and mf.vertexResourceId != rd.ResourceId():
            data = bytes(controller.GetBufferData(mf.vertexResourceId, mf.vertexByteOffset, 256))
            import struct
            n = len(data) // 4
            out["firstFloats"] = list(struct.unpack(f"<{min(n, 24)}f", data[: min(n, 24) * 4]))
    except Exception:
        pass
    return out


def dump_geometry(capture: str, event_id: int, stage_name: str) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        controller.SetFrameEvent(int(event_id), True)
        stage = _STAGE_ENUM.get(stage_name)
        if stage is None:
            return {"error": f"unknown stage: {stage_name}", "supported": list(_STAGE_ENUM.keys())}
        mf = controller.GetPostVSData(0, 0, stage)
        return {"eventId": event_id, "stage": stage_name, "geometry": _meshformat_summary(controller, mf)}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def compare_geometry(capture: str, event_left: int, event_right: int, stage_name: str) -> dict:
    a = dump_geometry(capture, event_left, stage_name)
    b = dump_geometry(capture, event_right, stage_name)
    if "error" in a or "error" in b:
        return {"left": a, "right": b}
    ga = a["geometry"]
    gb = b["geometry"]
    deltas = []
    for k in ("numIndices", "numVertices", "vertexByteStride", "topology"):
        if ga.get(k) != gb.get(k):
            deltas.append({"field": k, "left": ga.get(k), "right": gb.get(k)})
    # First-floats fingerprint: per-component diff
    la = ga.get("firstFloats") or []
    lb = gb.get("firstFloats") or []
    if la and lb:
        comp = []
        for i in range(min(len(la), len(lb))):
            if abs(la[i] - lb[i]) > 1e-7:
                comp.append({"index": i, "left": la[i], "right": lb[i], "delta": lb[i] - la[i]})
        if comp:
            deltas.append({"field": "firstFloats", "differences": comp[:64]})
    return {
        "left": a, "right": b, "deltas": deltas,
        "identical": not bool(deltas),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="PostVS geometry per event + diff.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dump")
    pd.add_argument("capture")
    pd.add_argument("--event", "-e", type=int, required=True)
    pd.add_argument("--stage", default="VSOut", choices=list(_STAGE_ENUM.keys()))

    pc = sub.add_parser("compare")
    pc.add_argument("capture")
    pc.add_argument("--left", type=int, required=True)
    pc.add_argument("--right", type=int, required=True)
    pc.add_argument("--stage", default="VSOut", choices=list(_STAGE_ENUM.keys()))

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.cmd == "dump":
            out = dump_geometry(args.capture, args.event, args.stage)
        else:
            out = compare_geometry(args.capture, args.left, args.right, args.stage)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
