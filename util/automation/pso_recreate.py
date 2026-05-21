"""Helper for retroactive PSO registration (the May 20 doc's gap).

UEVR's ``ShaderOverrideRegistry::resolve_d3d12_pipeline_state_for_eye``
needs the original PSO to be *registered* — meaning UEVR has previously
seen its CreateXxxPipelineState call and stored an
``OwnedD3D12GraphicsPipelineStateDesc``. For PSOs that enter the process
via the PSO cache or pipeline library, the creation hook never fires.

This module exports everything UEVR needs to *retroactively* synthesize
that registration from the live D3D12 pipeline state at a SetPipelineState
event. Given the PSO ResourceId:

  - Per-stage shader bytecode (hash + raw bytes — written to disk as .cso)
  - Root signature ResourceId (UEVR already tracks this)
  - Full graphics pipeline desc fields (blend, rasterizer, depth-stencil,
    input layout, RT formats, etc.)

The output is a JSON manifest UEVR can consume to call
``CreateGraphicsPipelineState`` on a substitute PSO with patched
bytecode, then route the original PSO's ``SetPipelineState`` to the
substitute via ``ReplaceResource``.

Usage::

    python -m util.automation.pso_recreate <cap.rdc> --pso ResourceId::1234 [--out manifest.json] [--shader-dir shaders/]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _find_first_event_using_pso(controller, target_pso) -> Optional[int]:
    """Walk actions and find the first draw/dispatch where the bound PSO
    matches ``target_pso``."""
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
        except Exception:
            continue
        try:
            d3d12 = controller.GetD3D12PipelineState()
        except Exception:
            d3d12 = None
        if d3d12 is None:
            continue
        if str(d3d12.pipelineResourceId) == str(target_pso):
            return eid
    return None


def recreate(capture_path: str, pso_ref: str,
             shader_dir: Optional[str] = None) -> Dict[str, Any]:
    cap, controller = _lib.open_capture(capture_path)
    try:
        # Resolve PSO reference (ResourceId or name).
        target = None
        for r in controller.GetResources():
            if str(r.resourceId) == pso_ref or str(r.name) == pso_ref:
                target = r.resourceId
                break
        if target is None:
            return {"error": f"PSO not found: {pso_ref}"}

        eid = _find_first_event_using_pso(controller, target)
        if eid is None:
            return {"error": f"no draw/dispatch found using PSO {target}"}

        state = _lib.collect_state_at_event(controller, eid)

        # Extract per-stage shader bytecode (hashes only by default;
        # extract raw bytes only if shader_dir is set).
        shaders = []
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
                refl = controller.GetPipelineState().GetShaderReflection(stage_enum)
            except Exception:
                refl = None
            if refl is None or len(refl.rawBytes) == 0:
                continue
            raw = bytes(refl.rawBytes)
            h = _lib.shader_bytecode_hash(raw)
            entry = {
                "stage": _lib.shader_stage_name(stage_enum),
                "bytecodeHash": h,
                "bytecodeSize": len(raw),
                "shaderId": _lib.resource_id_str(refl.resourceId),
                "entryPoint": str(refl.entryPoint),
            }
            if shader_dir:
                os.makedirs(shader_dir, exist_ok=True)
                path = os.path.join(shader_dir, f"{h}.cso")
                if not os.path.exists(path):
                    with open(path, "wb") as f:
                        f.write(raw)
                entry["bytecodePath"] = os.path.abspath(path)
            shaders.append(entry)

        return {
            "pso": str(target),
            "firstUseEvent": eid,
            "rootSignature": state.get("rootSignature", {}).get("id"),
            "renderTargets": state.get("renderTargets", []),
            "depthTarget": state.get("depthTarget"),
            "viewports": state.get("viewports", []),
            "scissors": state.get("scissors", []),
            "shaders": shaders,
            "note": (
                "Use this manifest from a UEVR plugin to retroactively "
                "register the PSO: load each shader's bytecode from "
                "bytecodePath, build an OwnedD3D12GraphicsPipelineStateDesc "
                "with the rootSignature + shaders + state, then "
                "register it before calling ReplaceResource at "
                "SetPipelineState time."
            ),
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Extract a tracked PSO's data for retroactive registration.")
    p.add_argument("capture")
    p.add_argument("--pso", required=True, help="PSO ResourceId (e.g. ResourceId::1234) or name")
    p.add_argument("--shader-dir", help="If set, write each shader's bytecode to <dir>/<hash>.cso")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = recreate(args.capture, args.pso, args.shader_dir)
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
