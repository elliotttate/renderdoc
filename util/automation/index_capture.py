"""Headless capture indexer.

Walks every action in a capture and dumps a directory of JSONL/JSON tables that
mirror the schema in docs/UEVR_NSIGHT_AUTOMATION_ROADMAP.md (sections 19, 22).

Usage:
    python -m util.automation.index_capture <capture.rdc> --out <dir>

Or if Python doesn't have the package on path:
    python util/automation/index_capture.py <capture.rdc> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running both as module and as script
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


INDEXER_VERSION = "0.1.0"


def index_capture(capture_path: str, out_dir: str) -> int:
    cap, controller = _lib.open_capture(capture_path)
    try:
        os.makedirs(out_dir, exist_ok=True)
        _write_meta(controller, capture_path, out_dir)
        _write_resources(controller, out_dir)
        _write_events_and_state(controller, out_dir)
    finally:
        controller.Shutdown()
        cap.Shutdown()
    return 0


def _write_meta(controller, capture_path: str, out_dir: str) -> None:
    props = controller.GetAPIProperties()
    meta = {
        "indexer_version": INDEXER_VERSION,
        "capture_path": os.path.abspath(capture_path),
        "api": str(props.pipelineType).split(".")[-1] if hasattr(props, "pipelineType") else str(props),
        "vendor": str(props.vendor).split(".")[-1] if hasattr(props, "vendor") else None,
        "rgpCapture": bool(props.rgpCapture) if hasattr(props, "rgpCapture") else None,
        "shaderDebugging": bool(props.shaderDebugging) if hasattr(props, "shaderDebugging") else None,
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _write_resources(controller, out_dir: str) -> None:
    resources = []
    for r in controller.GetResources():
        resources.append(
            {
                "id": _lib.resource_id_str(r.resourceId),
                "name": str(r.name),
                "type": str(r.type).split(".")[-1] if hasattr(r, "type") else None,
            }
        )
    textures = []
    for t in controller.GetTextures():
        textures.append(
            {
                "id": _lib.resource_id_str(t.resourceId),
                "name": str(t.name) if hasattr(t, "name") else None,
                "type": str(t.type).split(".")[-1],
                "format": str(t.format.Name()) if hasattr(t.format, "Name") else str(t.format),
                "width": int(t.width),
                "height": int(t.height),
                "depth": int(t.depth),
                "arraysize": int(t.arraysize),
                "mips": int(t.mips),
                "samples": int(t.msSamp),
                "byteSize": int(t.byteSize) if hasattr(t, "byteSize") else 0,
            }
        )
    buffers = []
    for b in controller.GetBuffers():
        buffers.append(
            {
                "id": _lib.resource_id_str(b.resourceId),
                "length": int(b.length),
                "creationFlags": str(b.creationFlags).split(".")[-1] if hasattr(b, "creationFlags") else None,
            }
        )
    with open(os.path.join(out_dir, "resources.json"), "w", encoding="utf-8") as f:
        json.dump({"resources": resources, "textures": textures, "buffers": buffers}, f, indent=2)


def _write_events_and_state(controller, out_dir: str) -> None:
    shaders_dir = os.path.join(out_dir, "shaders")
    os.makedirs(shaders_dir, exist_ok=True)
    seen_shaders = set()

    events_path = os.path.join(out_dir, "events.jsonl")
    actions_path = os.path.join(out_dir, "actions.jsonl")
    state_path = os.path.join(out_dir, "state.jsonl")

    n_events = 0
    n_actions = 0

    structured = controller.GetStructuredFile()

    with _lib.JsonlWriter(events_path) as ev_w, _lib.JsonlWriter(actions_path) as ac_w, _lib.JsonlWriter(state_path) as st_w:
        for action in _lib.walk_actions(controller):
            ev_id = int(action.eventId)
            n_events += 1
            name = str(action.GetName(structured))
            parent_eid = int(action.parent.eventId) if action.parent is not None else None
            flags = _lib.action_flag_names(action.flags)

            ev_row = {
                "eventId": ev_id,
                "actionId": int(action.actionId),
                "name": name,
                "flags": flags,
                "parentEventId": parent_eid,
                "numChildren": len(action.children),
            }
            ev_w.write(ev_row)

            is_significant = any(
                f in flags
                for f in (
                    "Drawcall",
                    "Dispatch",
                    "Copy",
                    "Resolve",
                    "Clear",
                    "Indirect",
                    "GenMips",
                    "MeshDispatch",
                )
            )

            if is_significant:
                n_actions += 1
                ac_row = {
                    "eventId": ev_id,
                    "name": name,
                    "flags": flags,
                    "numIndices": int(action.numIndices),
                    "numInstances": int(action.numInstances),
                    "indexOffset": int(action.indexOffset),
                    "vertexOffset": int(action.vertexOffset),
                    "instanceOffset": int(action.instanceOffset),
                    "baseVertex": int(action.baseVertex),
                    "dispatchDim": list(action.dispatchDimension),
                    "dispatchThreads": list(action.dispatchThreadsDimension),
                    "copySource": _lib.resource_id_str(action.copySource),
                    "copyDestination": _lib.resource_id_str(action.copyDestination),
                    "outputs": [
                        _lib.resource_id_str(o)
                        for o in action.outputs
                        if _lib.resource_id_str(o)
                    ],
                    "depthOut": _lib.resource_id_str(action.depthOut),
                }
                ac_w.write(ac_row)

                # Snapshot state at this event (descriptor resolution etc.)
                try:
                    state = _lib.collect_state_at_event(controller, ev_id)
                except Exception as exc:
                    state = {"eventId": ev_id, "error": f"collect_state_at_event failed: {exc}"}
                st_w.write(state)

                # Export shader bytecode for any newly-seen shader hash
                for s in state.get("shaders", []):
                    h = s.get("bytecodeHash")
                    if not h or h == "empty" or h in seen_shaders:
                        continue
                    seen_shaders.add(h)
                    _export_shader(controller, s, shaders_dir)

    # Update meta with counts
    meta_path = os.path.join(out_dir, "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    meta["event_count"] = n_events
    meta["action_count"] = n_actions
    meta["shader_count"] = len(seen_shaders)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _export_shader(controller, shader_meta: dict, shaders_dir: str) -> None:
    """Save raw bytecode + a short reflection summary per unique shader hash."""
    h = shader_meta["bytecodeHash"]
    prefix = h[:2]
    out_subdir = os.path.join(shaders_dir, prefix)
    os.makedirs(out_subdir, exist_ok=True)

    # Find the reflection again — open at any event where this shader is bound.
    # Cheap path: re-query the current state, which we just set in collect_state_at_event.
    pipe = controller.GetPipelineState()
    refl = None
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
        try:
            r = pipe.GetShaderReflection(stage_enum)
        except Exception:
            r = None
        if r is not None and _lib.shader_bytecode_hash(bytes(r.rawBytes)) == h:
            refl = r
            break
    if refl is None:
        return

    bin_path = os.path.join(out_subdir, f"{h}.bin")
    with open(bin_path, "wb") as f:
        f.write(bytes(refl.rawBytes))

    def _bindings(arr):
        return [
            {
                "name": str(b.name),
                "descriptorType": str(b.descriptorType).split(".")[-1] if hasattr(b, "descriptorType") else None,
                "register": int(b.fixedBindNumber),
                "space": int(b.fixedBindSetOrSpace),
                "isReadOnly": bool(b.isReadOnly) if hasattr(b, "isReadOnly") else None,
                "isTexture": bool(b.isTexture) if hasattr(b, "isTexture") else None,
            }
            for b in arr
        ]

    refl_json = {
        "bytecodeHash": h,
        "shaderId": _lib.resource_id_str(refl.resourceId),
        "stage": _lib.shader_stage_name(refl.stage),
        "entryPoint": str(refl.entryPoint),
        "encoding": str(refl.encoding).split(".")[-1],
        "bytecodeSize": len(refl.rawBytes),
        "dispatchThreadsDim": list(refl.dispatchThreadsDimension),
        "constantBlocks": _bindings(refl.constantBlocks),
        "samplers": _bindings(refl.samplers),
        "readOnlyResources": _bindings(refl.readOnlyResources),
        "readWriteResources": _bindings(refl.readWriteResources),
    }
    with open(os.path.join(out_subdir, f"{h}.json"), "w", encoding="utf-8") as f:
        json.dump(refl_json, f, indent=2)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Headless RenderDoc capture indexer (UEVR/Nsight automation).")
    p.add_argument("capture", help="Path to .rdc capture file")
    p.add_argument("--out", "-o", required=True, help="Output directory for the index")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        return index_capture(args.capture, args.out)
    finally:
        rd.ShutdownReplay()


if __name__ == "__main__":
    sys.exit(main())
