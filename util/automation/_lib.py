"""Shared helpers for util/automation tools.

Provides:
  - open_capture(path) -> (cap, controller)
  - walk_actions(controller) -> generator of every ActionDescription, depth-first
  - shader_bytecode_hash(rawBytes) -> stable short hash string
  - to_jsonable(value) -> recursively convert RenderDoc API objects to plain
    Python/JSON-safe values
  - JsonlWriter helper
  - register_type/descriptor_type stringification
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any, Iterator, Optional, Tuple


def _ensure_renderdoc():
    if "renderdoc" not in sys.modules and "_renderdoc" not in sys.modules:
        import renderdoc  # noqa: F401  (importing triggers binding load)


_ensure_renderdoc()
import renderdoc as rd  # type: ignore  # noqa: E402


def open_capture(path: str) -> Tuple[Any, Any]:
    """Open a .rdc file and return (cap, controller).

    Caller is responsible for shutdown:
        controller.Shutdown(); cap.Shutdown()
    """
    cap = rd.OpenCaptureFile()
    res = cap.OpenFile(path, "", None)
    if res != rd.ResultCode.Succeeded:
        raise RuntimeError(f"OpenFile({path}) failed: {res}")
    if not cap.LocalReplaySupport():
        raise RuntimeError(f"{path}: local replay not supported")
    res, controller = cap.OpenCapture(rd.ReplayOptions(), None)
    if res != rd.ResultCode.Succeeded:
        raise RuntimeError(f"OpenCapture({path}) failed: {res}")
    return cap, controller


def walk_actions(controller) -> Iterator[Any]:
    """Yield every ActionDescription in the capture in depth-first event order."""

    def _rec(action):
        yield action
        for child in action.children:
            yield from _rec(child)

    for root in controller.GetRootActions():
        yield from _rec(root)


def shader_bytecode_hash(raw: bytes) -> str:
    """Stable hash for shader bytecode.

    RenderDoc does not expose a stable per-shader hash via ShaderReflection,
    so we derive one from rawBytes. MD5 hex (32 chars) — chosen to match the
    md5 helper already vendored in renderdoc/3rdparty/md5/ so the C++
    renderdoccmd subcommand emits identical hashes.
    """
    if raw is None or len(raw) == 0:
        return "empty"
    return hashlib.md5(bytes(raw)).hexdigest()


# ----- Stringification ------------------------------------------------------


def descriptor_type_name(t) -> str:
    return str(t).split(".")[-1] if t is not None else "Unknown"


def descriptor_category_name(c) -> str:
    return str(c).split(".")[-1] if c is not None else "Unknown"


def shader_stage_name(s) -> str:
    return str(s).split(".")[-1] if s is not None else "Unknown"


def action_flag_names(flags) -> list:
    """Decompose an ActionFlags bitfield to a list of named flags."""
    names = []
    for n in (
        "Clear",
        "Drawcall",
        "Dispatch",
        "CmdList",
        "SetMarker",
        "PushMarker",
        "PopMarker",
        "Present",
        "MultiAction",
        "Copy",
        "Resolve",
        "GenMips",
        "PassBoundary",
        "Indexed",
        "Instanced",
        "Auto",
        "Indirect",
        "ClearColor",
        "ClearDepthStencil",
        "BeginPass",
        "EndPass",
        "CommandBufferBoundary",
        "MeshDispatch",
    ):
        flag = getattr(rd.ActionFlags, n, None)
        if flag is None:
            continue
        if int(flags) & int(flag):
            names.append(n)
    return names


def resource_id_str(rid) -> Optional[str]:
    """Return the ResourceId as a string, or None for invalid/zero IDs."""
    if rid is None:
        return None
    s = str(rid)
    if s in ("ResourceId()", "0"):
        return None
    return s


# ----- Generic to_jsonable --------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Recursively convert RenderDoc objects to JSON-safe values."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    # rdcarray
    try:
        return [to_jsonable(v) for v in list(value)]
    except TypeError:
        pass
    s = str(value)
    return s


# ----- JSONL writer ---------------------------------------------------------


class JsonlWriter:
    """Append-only JSON-Lines writer."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._f = open(path, "w", encoding="utf-8", newline="\n")

    def write(self, obj) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        self._f.write("\n")

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ----- Descriptor + binding extraction --------------------------------------


def used_descriptor_to_dict(used, reflection_by_stage: dict) -> dict:
    """Flatten a UsedDescriptor into a dict referencing the shader register if known.

    reflection_by_stage maps shader stage -> ShaderReflection (or None) so we
    can look up the register / space from access.index.
    """
    access = used.access
    stage = shader_stage_name(access.stage)
    typ = descriptor_type_name(access.type)

    register = None
    space = None
    name = None
    reflection = reflection_by_stage.get(stage)
    if reflection is not None and access.index != 0xFFFF:
        bind = _shader_binding_from_index(reflection, access.type, access.index)
        if bind is not None:
            register = int(bind.fixedBindNumber)
            space = int(bind.fixedBindSetOrSpace)
            name = str(bind.name)

    descriptor = used.descriptor
    sampler = used.sampler
    out = {
        "stage": stage,
        "type": typ,
        "register": register,
        "space": space,
        "name": name,
        "arrayElement": int(access.arrayElement),
        "staticallyUnused": bool(access.staticallyUnused),
        "heap": resource_id_str(access.descriptorStore),
        "heapByteOffset": int(access.byteOffset),
        "byteSize": int(access.byteSize),
    }
    if descriptor is not None and resource_id_str(descriptor.resource) is not None:
        out["resource"] = resource_id_str(descriptor.resource)
        out["view"] = resource_id_str(descriptor.view)
        out["format"] = str(descriptor.format.Name()) if hasattr(descriptor.format, "Name") else str(descriptor.format)
        out["firstMip"] = int(descriptor.firstMip)
        out["numMips"] = int(descriptor.numMips)
        out["firstSlice"] = int(descriptor.firstSlice)
        out["numSlices"] = int(descriptor.numSlices)
        out["bufferByteOffset"] = int(descriptor.byteOffset)
        out["bufferByteSize"] = int(descriptor.byteSize)
    if sampler is not None and resource_id_str(sampler.object) is not None:
        out["sampler"] = resource_id_str(sampler.object)
    return out


def _shader_binding_from_index(reflection, descriptor_type, index: int):
    """Look up the ShaderResource/ConstantBlock/Sampler at the given index."""
    category = rd.CategoryForDescriptorType(descriptor_type) if hasattr(rd, "CategoryForDescriptorType") else None
    cat_name = descriptor_category_name(category) if category is not None else descriptor_type_name(descriptor_type)
    try:
        idx = int(index)
        if cat_name == "ConstantBlock" or descriptor_type_name(descriptor_type) == "ConstantBuffer":
            arr = reflection.constantBlocks
        elif cat_name == "Sampler" or descriptor_type_name(descriptor_type) == "Sampler":
            arr = reflection.samplers
        elif cat_name == "ReadWriteResource" or descriptor_type_name(descriptor_type).startswith("ReadWrite"):
            arr = reflection.readWriteResources
        else:
            arr = reflection.readOnlyResources
        if 0 <= idx < len(arr):
            return arr[idx]
    except Exception:
        pass
    return None


def collect_state_at_event(controller, event_id: int) -> dict:
    """Snapshot useful pipeline state at the given event. API-agnostic where possible."""
    controller.SetFrameEvent(event_id, True)
    pipe = controller.GetPipelineState()
    out = {"eventId": int(event_id)}

    out["api"] = str(pipe.m_PipelineType) if hasattr(pipe, "m_PipelineType") else "Unknown"

    # Per-stage reflection lookup (used for register/space resolution)
    reflection_by_stage = {}
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
            refl = pipe.GetShaderReflection(stage_enum)
        except Exception:
            refl = None
        reflection_by_stage[shader_stage_name(stage_enum)] = refl

    # Shaders
    shaders = []
    for stage_enum, refl in (
        (rd.ShaderStage.Vertex, reflection_by_stage["Vertex"]),
        (rd.ShaderStage.Hull, reflection_by_stage["Hull"]),
        (rd.ShaderStage.Domain, reflection_by_stage["Domain"]),
        (rd.ShaderStage.Geometry, reflection_by_stage["Geometry"]),
        (rd.ShaderStage.Pixel, reflection_by_stage["Pixel"]),
        (rd.ShaderStage.Compute, reflection_by_stage["Compute"]),
        (rd.ShaderStage.Amplification, reflection_by_stage["Amplification"]),
        (rd.ShaderStage.Mesh, reflection_by_stage["Mesh"]),
    ):
        if refl is None:
            continue
        shaders.append(
            {
                "stage": shader_stage_name(stage_enum),
                "shaderId": resource_id_str(refl.resourceId),
                "entryPoint": str(refl.entryPoint),
                "encoding": str(refl.encoding).split(".")[-1],
                "bytecodeHash": shader_bytecode_hash(bytes(refl.rawBytes)),
                "bytecodeSize": len(refl.rawBytes),
            }
        )
    out["shaders"] = shaders

    # Viewports / scissors (api-agnostic via D3D12 state for D3D12; otherwise omit)
    d3d12 = None
    try:
        d3d12 = controller.GetD3D12PipelineState()
    except Exception:
        d3d12 = None

    if d3d12 is not None and d3d12.pipelineResourceId is not None:
        out["pipelineId"] = resource_id_str(d3d12.pipelineResourceId)
        out["descriptorHeaps"] = [resource_id_str(h) for h in d3d12.descriptorHeaps if resource_id_str(h)]
        out["viewports"] = [
            {
                "x": float(v.x),
                "y": float(v.y),
                "width": float(v.width),
                "height": float(v.height),
                "minDepth": float(v.minDepth),
                "maxDepth": float(v.maxDepth),
            }
            for v in d3d12.rasterizer.viewports
        ]
        out["scissors"] = [
            {"x": int(s.x), "y": int(s.y), "width": int(s.width), "height": int(s.height)}
            for s in d3d12.rasterizer.scissors
        ]
        out["renderTargets"] = []
        for rt in d3d12.outputMerger.renderTargets:
            if resource_id_str(rt.resource) is None:
                continue
            out["renderTargets"].append(
                {
                    "resource": resource_id_str(rt.resource),
                    "view": resource_id_str(rt.view),
                    "format": str(rt.format.Name()) if hasattr(rt.format, "Name") else str(rt.format),
                    "firstMip": int(rt.firstMip),
                    "firstSlice": int(rt.firstSlice),
                    "numSlices": int(rt.numSlices),
                }
            )
        ds = d3d12.outputMerger
        if hasattr(ds, "depthStencilTarget") and resource_id_str(ds.depthStencilTarget.resource):
            out["depthTarget"] = {
                "resource": resource_id_str(ds.depthStencilTarget.resource),
                "view": resource_id_str(ds.depthStencilTarget.view),
                "format": str(ds.depthStencilTarget.format.Name())
                if hasattr(ds.depthStencilTarget.format, "Name")
                else str(ds.depthStencilTarget.format),
            }
        # Root signature
        rs = d3d12.rootSignature
        params = []
        for i, p in enumerate(rs.parameters):
            entry = {
                "index": i,
                "visibility": str(p.visibility).split(".")[-1],
                "space": int(p.space),
                "reg": int(p.reg),
            }
            if len(p.constants) > 0:
                entry["kind"] = "RootConstants"
                entry["bytes"] = bytes(p.constants).hex()
            elif len(p.tableRanges) > 0:
                entry["kind"] = "RootTable"
                entry["heap"] = resource_id_str(p.heap)
                entry["heapByteOffset"] = int(p.heapByteOffset)
                entry["ranges"] = [
                    {
                        "category": descriptor_category_name(r.category),
                        "space": int(r.space),
                        "baseRegister": int(r.baseRegister),
                        "count": int(r.count),
                        "tableByteOffset": int(r.tableByteOffset),
                        "appended": bool(r.appended),
                    }
                    for r in p.tableRanges
                ]
            else:
                entry["kind"] = "RootDescriptor"
                entry["resource"] = resource_id_str(p.descriptor.resource)
                entry["byteOffset"] = int(p.descriptor.byteOffset)
                entry["byteSize"] = int(p.descriptor.byteSize)
            params.append(entry)
        out["rootSignature"] = {
            "id": resource_id_str(rs.resourceId),
            "parameters": params,
        }

    # Used descriptors per stage
    bindings = []
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
                bindings.append(used_descriptor_to_dict(used, reflection_by_stage))
    out["bindings"] = bindings
    return out
