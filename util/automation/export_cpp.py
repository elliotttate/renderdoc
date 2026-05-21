"""Export a D3D12 capture as a compilable C++ source project.

This is RenderDoc's equivalent of Nsight Graphics' "Generate C++ Capture":
given a ``.rdc`` capture file, it walks the structured file (``SDFile``) and
emits a buildable Visual Studio / CMake project that reproduces the same
sequence of ``ID3D12Device`` / ``ID3D12CommandQueue`` /
``ID3D12GraphicsCommandList`` calls as the original frame.

Output layout::

    <out_dir>/
        main.cpp              # entry point + capture replay logic
        capture_frame.cpp     # the per-frame call sequence emitted from the SDFile
        capture_frame.h       # shared declarations
        CMakeLists.txt
        README.md
        shaders/<hash>.cso    # extracted shader bytecode blobs
        blobs/buf_<id>.bin    # extracted buffer initial contents
        unhandled.txt         # one line per unhandled chunk for triage

Scope
-----

The exporter covers the common D3D12 command surface needed to reproduce a
typical frame:

  - Device-level creation (CommandQueue, CommandAllocator, CommandList,
    DescriptorHeap, RootSignature, GraphicsPipeline, ComputePipeline,
    PipelineState stream, CommittedResource, PlacedResource, Heap, Fence,
    Sampler)
  - View creation (CBV/SRV/UAV/RTV/DSV)
  - Descriptor copies (CopyDescriptors, CopyDescriptorsSimple)
  - Command list recording (resource barriers, set state, draws, dispatches,
    clears, copies, raytracing dispatch, mesh dispatch, execute indirect)
  - Queue execution (ExecuteCommandLists, Signal, Wait, debug markers)

Anything else is emitted as a ``/* TODO unhandled chunk: <name> */`` comment so
the output still compiles structurally and the user can manually fill in the
gaps. The ``unhandled.txt`` file lists every skipped chunk in capture order.

Usage::

    python -m util.automation.export_cpp <capture.rdc> --out <dir>

CLI flags::

    --first-event <eid>    only emit chunks at or after this event ID
    --last-event <eid>     stop emitting after this event ID
    --no-blobs             skip extracting shader/buffer blobs (faster)
    --json-only            emit a JSON dump of all chunks instead of C++
"""

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import textwrap
from typing import Any, Callable, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


# ---------------------------------------------------------------------------
# SDObject helpers
# ---------------------------------------------------------------------------

def sd_child(obj, name: str):
    """Return the first child of ``obj`` named ``name``, or ``None``."""
    if obj is None:
        return None
    return obj.FindChild(name) if hasattr(obj, "FindChild") else None


def _basetype(obj):
    try:
        return str(obj.type.basetype).split(".")[-1]
    except Exception:
        return ""


def sd_is_resource(obj):
    return obj is not None and _basetype(obj) == "Resource"


def sd_is_string(obj):
    return obj is not None and _basetype(obj) == "String"


def sd_is_uint(obj):
    return obj is not None and _basetype(obj) == "UnsignedInteger"


def sd_is_int(obj):
    return obj is not None and _basetype(obj) == "SignedInteger"


def sd_is_float(obj):
    return obj is not None and _basetype(obj) == "Float"


def sd_is_bool(obj):
    return obj is not None and _basetype(obj) == "Boolean"


def sd_is_enum(obj):
    return obj is not None and _basetype(obj) == "Enum"


def sd_is_array(obj):
    return obj is not None and _basetype(obj) == "Array"


def sd_value(obj):
    """Return the most useful Python scalar for an SDObject."""
    if obj is None:
        return None
    bt = _basetype(obj)
    try:
        if bt == "Resource":
            return ("ResourceId", str(obj.AsResourceId()))
        if bt == "String":
            return str(obj.AsString())
        if bt == "Boolean":
            try:
                return bool(obj.AsBool())
            except Exception:
                return bool(obj.data.basic.b) if hasattr(obj, "data") else None
        if bt == "Float":
            try:
                return float(obj.AsFloat())
            except Exception:
                return float(obj.data.basic.d) if hasattr(obj, "data") else None
        if bt == "SignedInteger":
            return int(obj.AsInt64())
        if bt == "UnsignedInteger":
            return int(obj.AsUInt64())
        if bt == "Enum":
            try:
                return ("Enum", str(obj.AsString()))
            except Exception:
                return ("Enum", int(obj.AsUInt64()))
    except Exception:
        pass
    return None


def sd_to_python(obj, max_depth: int = 8):
    """Recursively convert an SDObject to a JSON-safe Python value.

    Structs become dicts keyed by child name; arrays become lists; leaves use
    ``sd_value``. ``max_depth`` is a safety cap.
    """
    if obj is None or max_depth <= 0:
        return None
    try:
        n_children = obj.NumChildren()
    except Exception:
        n_children = 0
    if n_children == 0:
        return sd_value(obj)

    if sd_is_array(obj):
        return [sd_to_python(obj.GetChild(i), max_depth - 1) for i in range(n_children)]

    out = {}
    for i in range(n_children):
        c = obj.GetChild(i)
        if c is None:
            continue
        out[str(c.name)] = sd_to_python(c, max_depth - 1)
    return out


def sd_uint(obj, name: str, default: int = 0) -> int:
    c = sd_child(obj, name)
    try:
        return int(c.AsUInt64()) if c is not None else default
    except Exception:
        return default


def sd_int(obj, name: str, default: int = 0) -> int:
    c = sd_child(obj, name)
    try:
        return int(c.AsInt64()) if c is not None else default
    except Exception:
        return default


def sd_float(obj, name: str, default: float = 0.0) -> float:
    c = sd_child(obj, name)
    try:
        return float(c.AsFloat()) if c is not None else default
    except Exception:
        return default


def sd_resource(obj, name: str):
    c = sd_child(obj, name)
    if c is None:
        return None
    try:
        return c.AsResourceId()
    except Exception:
        return None


def sd_enum_str(obj, name: str, default: str = "") -> str:
    c = sd_child(obj, name)
    if c is None:
        return default
    try:
        return str(c.AsString())
    except Exception:
        try:
            return str(int(c.AsUInt64()))
        except Exception:
            return default


# ---------------------------------------------------------------------------
# C++ value formatting
# ---------------------------------------------------------------------------

def cpp_uint(v: int) -> str:
    return f"{int(v) & 0xFFFFFFFFFFFFFFFF}u" if v >= 0 else f"{v}"


def cpp_float(v: float) -> str:
    # Use enough precision to round-trip; avoid scientific notation for nicer C++.
    s = f"{float(v):.9g}"
    if "e" in s or "E" in s or "." in s or "inf" in s or "nan" in s.lower():
        if "inf" in s.lower() or "nan" in s.lower():
            return "0.0f /* " + s + " */"
        return s + "f"
    return s + ".0f"


def cpp_string(s: str) -> str:
    """Escape ``s`` as a C++ string literal."""
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\"":
            out.append("\\\"")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 0x20 <= ord(ch) < 0x7F:
            out.append(ch)
        else:
            out.append(f"\\x{ord(ch):02x}")
    return '"' + "".join(out) + '"'


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def safe_ident(prefix: str, raw: str) -> str:
    return prefix + _SAFE_NAME_RE.sub("_", raw)


class NameTable:
    """Maps RenderDoc ResourceIds to stable C++ identifier names.

    Names are deterministic given the same input capture so that re-running
    the exporter produces a diffable output.
    """

    # Prefix by D3D12 type, derived from the resource description's type field
    # if present, else by the kind of chunk that introduced it.
    PREFIX_BY_KIND = {
        "Buffer": "buf",
        "Texture1D": "tex1d",
        "Texture2D": "tex2d",
        "Texture3D": "tex3d",
        "TextureCube": "texcube",
        "CommandQueue": "queue",
        "CommandAllocator": "alloc",
        "CommandList": "cmd",
        "PipelineState": "pso",
        "RootSignature": "rs",
        "DescriptorHeap": "heap",
        "Heap": "memheap",
        "Fence": "fence",
        "QueryHeap": "queryheap",
        "Sampler": "samp",
        "Shader": "shader",
        "StateObject": "state",
        "CommandSignature": "cmdsig",
        "Swapchain": "swap",
        "Unknown": "obj",
    }

    def __init__(self):
        self._by_id: Dict[str, str] = {}
        self._counters: Dict[str, int] = {}

    def assign(self, rid, kind: str = "Unknown") -> str:
        key = str(rid)
        if key in self._by_id:
            return self._by_id[key]
        prefix = self.PREFIX_BY_KIND.get(kind, "obj")
        n = self._counters.get(prefix, 0)
        self._counters[prefix] = n + 1
        name = f"{prefix}_{n:04d}"
        self._by_id[key] = name
        return name

    def get(self, rid, kind: str = "Unknown") -> Optional[str]:
        if rid is None:
            return None
        s = str(rid)
        if s in ("ResourceId()", "0"):
            return None
        if s in self._by_id:
            return self._by_id[s]
        return self.assign(rid, kind)

    def lookup(self, rid) -> Optional[str]:
        if rid is None:
            return None
        return self._by_id.get(str(rid))

    def lookup_by_str(self, rid_str: str) -> Optional[str]:
        return self._by_id.get(rid_str)

    def has(self, rid) -> bool:
        return rid is not None and str(rid) in self._by_id


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class ExportContext:
    """Per-export state shared across emitters."""

    def __init__(self, out_dir: str, controller=None):
        self.out_dir = out_dir
        self.controller = controller
        self.names = NameTable()
        self.lines: List[str] = []     # capture_frame.cpp body
        self.decls: List[str] = []     # capture_frame.h declarations
        self.unhandled: List[Tuple[int, str]] = []
        self.shader_blobs: Dict[str, bytes] = {}    # hash -> bytecode
        self.shader_id_to_hash: Dict[str, str] = {}  # ResourceId str -> hash
        self.buffer_blobs: Dict[str, bytes] = {}     # name -> bytes
        self.write_blobs = True
        self.first_event: Optional[int] = None
        self.last_event: Optional[int] = None
        self._chunks_emitted = 0
        self._last_cmdlist: Optional[str] = None

        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(os.path.join(out_dir, "shaders"), exist_ok=True)
        os.makedirs(os.path.join(out_dir, "blobs"), exist_ok=True)

    def emit(self, line: str) -> None:
        self.lines.append(line)

    def emit_section(self, header: str) -> None:
        self.lines.append("")
        self.lines.append(f"  // ----- {header} -----")

    def add_decl(self, line: str) -> None:
        self.decls.append(line)

    def unhandled_chunk(self, idx: int, name: str) -> None:
        self.unhandled.append((idx, name))
        self.lines.append(f"  /* TODO unhandled chunk #{idx}: {name} */")


# ---------------------------------------------------------------------------
# Emitter registry
# ---------------------------------------------------------------------------

EmitterFn = Callable[[ExportContext, Any], None]
EMITTERS: Dict[str, EmitterFn] = {}


def emitter(name: str) -> Callable[[EmitterFn], EmitterFn]:
    """Decorator that registers a function as the emitter for a chunk name."""

    def wrap(fn: EmitterFn) -> EmitterFn:
        EMITTERS[name] = fn
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Generic emitter helpers
# ---------------------------------------------------------------------------

def declare_resource(ctx: ExportContext, rid, kind: str, cpp_type: str) -> Optional[str]:
    """Allocate a variable for ``rid`` of D3D12 type ``cpp_type``. Returns the
    chosen variable name and adds a header declaration."""
    if rid is None or str(rid) in ("ResourceId()", "0"):
        return None
    if ctx.names.has(rid):
        return ctx.names.get(rid)
    name = ctx.names.assign(rid, kind)
    ctx.add_decl(f"extern ComPtr<{cpp_type}> {name};")
    return name


def use_resource(ctx: ExportContext, rid) -> str:
    """Return the C++ variable name for ``rid``, or a placeholder if unknown."""
    if rid is None:
        return "nullptr"
    s = str(rid)
    if s in ("ResourceId()", "0"):
        return "nullptr"
    name = ctx.names.lookup(rid)
    if name:
        return f"{name}.Get()"
    return f"/* unresolved {s} */ nullptr"


# Map enum string fragments to D3D12 token. RenderDoc stringifies enums via
# ToStr which gives the bare token name (e.g. "D3D12_HEAP_TYPE_DEFAULT"); we
# only have to strip surrounding whitespace.
def enum_token(s: Optional[str], fallback: str = "0") -> str:
    if s is None:
        return fallback
    s = str(s).strip()
    if not s:
        return fallback
    return s


# ---------------------------------------------------------------------------
# Device chunk emitters
# ---------------------------------------------------------------------------

@emitter("ID3D12Device::CreateCommandQueue")
def emit_create_command_queue(ctx: ExportContext, chunk) -> None:
    desc = sd_child(chunk, "pDesc")
    rid = sd_resource(chunk, "pCommandQueue")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "CommandQueue", "ID3D12CommandQueue")
    type_ = sd_enum_str(desc, "Type", "D3D12_COMMAND_LIST_TYPE_DIRECT")
    priority = sd_int(desc, "Priority", 0)
    flags = sd_enum_str(desc, "Flags", "D3D12_COMMAND_QUEUE_FLAG_NONE")
    node_mask = sd_uint(desc, "NodeMask", 0)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_COMMAND_QUEUE_DESC d = {{}};")
    ctx.emit(f"    d.Type = {enum_token(type_)};")
    ctx.emit(f"    d.Priority = {priority};")
    ctx.emit(f"    d.Flags = {enum_token(flags)};")
    ctx.emit(f"    d.NodeMask = {node_mask};")
    ctx.emit(f"    HR(device->CreateCommandQueue(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateCommandAllocator")
def emit_create_command_allocator(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pCommandAllocator")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "CommandAllocator", "ID3D12CommandAllocator")
    typ = sd_enum_str(chunk, "type", "D3D12_COMMAND_LIST_TYPE_DIRECT")
    ctx.emit(f"  HR(device->CreateCommandAllocator({enum_token(typ)}, IID_PPV_ARGS(&{name})));")


@emitter("ID3D12Device::CreateCommandList")
def emit_create_command_list(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pCommandList")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "CommandList", "ID3D12GraphicsCommandList")
    typ = sd_enum_str(chunk, "type", "D3D12_COMMAND_LIST_TYPE_DIRECT")
    node_mask = sd_uint(chunk, "nodeMask", 0)
    alloc = sd_resource(chunk, "pCommandAllocator")
    pso = sd_resource(chunk, "pInitialState")
    alloc_var = use_resource(ctx, alloc)
    pso_var = use_resource(ctx, pso)
    ctx.emit(
        f"  HR(device->CreateCommandList({node_mask}, {enum_token(typ)}, {alloc_var}, {pso_var}, "
        f"IID_PPV_ARGS(&{name})));"
    )


@emitter("ID3D12Device4::CreateCommandList1")
def emit_create_command_list1(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pCommandList")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "CommandList", "ID3D12GraphicsCommandList1")
    typ = sd_enum_str(chunk, "type", "D3D12_COMMAND_LIST_TYPE_DIRECT")
    flags = sd_enum_str(chunk, "flags", "D3D12_COMMAND_LIST_FLAG_NONE")
    node_mask = sd_uint(chunk, "nodeMask", 0)
    ctx.emit(
        f"  {{ ComPtr<ID3D12Device4> d4; HR(device.As(&d4));"
        f" HR(d4->CreateCommandList1({node_mask}, {enum_token(typ)}, {enum_token(flags)}, "
        f"IID_PPV_ARGS(&{name}))); }}"
    )


@emitter("ID3D12Device::CreateDescriptorHeap")
def emit_create_descriptor_heap(ctx: ExportContext, chunk) -> None:
    desc = sd_child(chunk, "pDescriptorHeapDesc")
    rid = sd_resource(chunk, "pHeap")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "DescriptorHeap", "ID3D12DescriptorHeap")
    typ = sd_enum_str(desc, "Type", "D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV")
    count = sd_uint(desc, "NumDescriptors", 0)
    flags = sd_enum_str(desc, "Flags", "D3D12_DESCRIPTOR_HEAP_FLAG_NONE")
    node_mask = sd_uint(desc, "NodeMask", 0)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_DESCRIPTOR_HEAP_DESC d = {{}};")
    ctx.emit(f"    d.Type = {enum_token(typ)};")
    ctx.emit(f"    d.NumDescriptors = {count};")
    ctx.emit(f"    d.Flags = {enum_token(flags)};")
    ctx.emit(f"    d.NodeMask = {node_mask};")
    ctx.emit(f"    HR(device->CreateDescriptorHeap(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateRootSignature")
def emit_create_root_signature(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pRootSignature")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "RootSignature", "ID3D12RootSignature")
    node_mask = sd_uint(chunk, "nodeMask", 0)
    blob = sd_child(chunk, "pBlobWithRootSignature")
    blob_size = sd_uint(chunk, "blobLengthInBytes", 0)
    # We cannot reconstitute the blob from the chunk without raw bytes. Emit a
    # placeholder + comment so the user can wire up an external .cso/.bin.
    placeholder = f"/* TODO: load {blob_size}-byte serialized root signature blob */"
    ctx.emit(
        f"  HR(device->CreateRootSignature({node_mask}, /* pBlob */ nullptr {placeholder},"
        f" {blob_size}, IID_PPV_ARGS(&{name})));"
    )


@emitter("ID3D12Device::CreateGraphicsPipelineState")
def emit_create_graphics_pso(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pPipelineState")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "PipelineState", "ID3D12PipelineState")
    ctx.emit(f"  /* TODO populate D3D12_GRAPHICS_PIPELINE_STATE_DESC for {name} */")
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_GRAPHICS_PIPELINE_STATE_DESC d = {{}};")
    rs = sd_resource(sd_child(chunk, "pDesc"), "pRootSignature")
    if rs is not None:
        ctx.emit(f"    d.pRootSignature = {use_resource(ctx, rs)};")
    ctx.emit(f"    // ... shaders, input layout, render target formats, blend, etc.")
    ctx.emit(f"    HR(device->CreateGraphicsPipelineState(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateComputePipelineState")
def emit_create_compute_pso(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pPipelineState")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "PipelineState", "ID3D12PipelineState")
    rs = sd_resource(sd_child(chunk, "pDesc"), "pRootSignature")
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_COMPUTE_PIPELINE_STATE_DESC d = {{}};")
    if rs is not None:
        ctx.emit(f"    d.pRootSignature = {use_resource(ctx, rs)};")
    ctx.emit(f"    /* TODO populate CS bytecode for {name} */")
    ctx.emit(f"    HR(device->CreateComputePipelineState(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device2::CreatePipelineState")
def emit_create_pipeline_state_stream(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pPipelineState")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "PipelineState", "ID3D12PipelineState")
    ctx.emit(
        f"  /* TODO PipelineStateStream PSO {name}: reconstruct subobjects from chunk */"
    )


@emitter("ID3D12Device::CreateCommittedResource")
@emitter("ID3D12Device4::CreateCommittedResource1")
@emitter("ID3D12Device8::CreateCommittedResource2")
@emitter("ID3D12Device10::CreateCommittedResource3")
def emit_create_committed_resource(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pResource")
    if rid is None:
        return
    desc = sd_child(chunk, "pDesc")
    dim = sd_enum_str(desc, "Dimension", "D3D12_RESOURCE_DIMENSION_BUFFER")
    if "BUFFER" in str(dim):
        kind = "Buffer"
    elif "TEXTURE1D" in str(dim):
        kind = "Texture1D"
    elif "TEXTURE2D" in str(dim):
        kind = "Texture2D"
    elif "TEXTURE3D" in str(dim):
        kind = "Texture3D"
    else:
        kind = "Buffer"
    name = declare_resource(ctx, rid, kind, "ID3D12Resource")

    props = sd_child(chunk, "pHeapProperties")
    heap_flags = sd_enum_str(chunk, "HeapFlags", "D3D12_HEAP_FLAG_NONE")
    initial_state = sd_enum_str(chunk, "InitialResourceState", "D3D12_RESOURCE_STATE_COMMON")

    width = sd_uint(desc, "Width", 0)
    height = sd_uint(desc, "Height", 0)
    depth = sd_uint(desc, "DepthOrArraySize", 1)
    mips = sd_uint(desc, "MipLevels", 1)
    fmt = sd_enum_str(desc, "Format", "DXGI_FORMAT_UNKNOWN")
    samples = sd_uint(sd_child(desc, "SampleDesc"), "Count", 1)
    layout = sd_enum_str(desc, "Layout", "D3D12_TEXTURE_LAYOUT_UNKNOWN")
    flags = sd_enum_str(desc, "Flags", "D3D12_RESOURCE_FLAG_NONE")
    alignment = sd_uint(desc, "Alignment", 0)

    heap_type = sd_enum_str(props, "Type", "D3D12_HEAP_TYPE_DEFAULT")
    cpu_page = sd_enum_str(props, "CPUPageProperty", "D3D12_CPU_PAGE_PROPERTY_UNKNOWN")
    mem_pool = sd_enum_str(props, "MemoryPoolPreference", "D3D12_MEMORY_POOL_UNKNOWN")
    creation_node = sd_uint(props, "CreationNodeMask", 0)
    visible_node = sd_uint(props, "VisibleNodeMask", 0)

    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_HEAP_PROPERTIES p = {{}};")
    ctx.emit(f"    p.Type = {enum_token(heap_type)};")
    ctx.emit(f"    p.CPUPageProperty = {enum_token(cpu_page)};")
    ctx.emit(f"    p.MemoryPoolPreference = {enum_token(mem_pool)};")
    ctx.emit(f"    p.CreationNodeMask = {creation_node};")
    ctx.emit(f"    p.VisibleNodeMask = {visible_node};")
    ctx.emit(f"    D3D12_RESOURCE_DESC d = {{}};")
    ctx.emit(f"    d.Dimension = {enum_token(dim)};")
    ctx.emit(f"    d.Alignment = {alignment};")
    ctx.emit(f"    d.Width = {width};")
    ctx.emit(f"    d.Height = {height};")
    ctx.emit(f"    d.DepthOrArraySize = {depth};")
    ctx.emit(f"    d.MipLevels = {mips};")
    ctx.emit(f"    d.Format = {enum_token(fmt)};")
    ctx.emit(f"    d.SampleDesc.Count = {samples};")
    ctx.emit(f"    d.Layout = {enum_token(layout)};")
    ctx.emit(f"    d.Flags = {enum_token(flags)};")
    ctx.emit(
        f"    HR(device->CreateCommittedResource(&p, {enum_token(heap_flags)}, &d, "
        f"{enum_token(initial_state)}, nullptr, IID_PPV_ARGS(&{name})));"
    )
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateHeap")
@emitter("ID3D12Device4::CreateHeap1")
def emit_create_heap(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pHeap")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "Heap", "ID3D12Heap")
    desc = sd_child(chunk, "pDesc")
    size = sd_uint(desc, "SizeInBytes", 0)
    alignment = sd_uint(desc, "Alignment", 0)
    flags = sd_enum_str(desc, "Flags", "D3D12_HEAP_FLAG_NONE")
    props = sd_child(desc, "Properties")
    heap_type = sd_enum_str(props, "Type", "D3D12_HEAP_TYPE_DEFAULT")
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_HEAP_DESC d = {{}};")
    ctx.emit(f"    d.SizeInBytes = {size};")
    ctx.emit(f"    d.Alignment = {alignment};")
    ctx.emit(f"    d.Properties.Type = {enum_token(heap_type)};")
    ctx.emit(f"    d.Flags = {enum_token(flags)};")
    ctx.emit(f"    HR(device->CreateHeap(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreatePlacedResource")
@emitter("ID3D12Device8::CreatePlacedResource1")
@emitter("ID3D12Device10::CreatePlacedResource2")
def emit_create_placed_resource(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pResource")
    if rid is None:
        return
    heap = sd_resource(chunk, "pHeap")
    offset = sd_uint(chunk, "HeapOffset", 0)
    initial_state = sd_enum_str(chunk, "InitialResourceState", "D3D12_RESOURCE_STATE_COMMON")
    desc = sd_child(chunk, "pDesc")
    dim = sd_enum_str(desc, "Dimension", "D3D12_RESOURCE_DIMENSION_BUFFER")
    kind = "Buffer" if "BUFFER" in str(dim) else "Texture2D"
    name = declare_resource(ctx, rid, kind, "ID3D12Resource")
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_RESOURCE_DESC d = {{}}; d.Dimension = {enum_token(dim)};")
    ctx.emit(f"    /* TODO populate full D3D12_RESOURCE_DESC for {name} */")
    ctx.emit(
        f"    HR(device->CreatePlacedResource({use_resource(ctx, heap)}, {offset}, &d, "
        f"{enum_token(initial_state)}, nullptr, IID_PPV_ARGS(&{name})));"
    )
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateFence")
def emit_create_fence(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pFence")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "Fence", "ID3D12Fence")
    initial = sd_uint(chunk, "InitialValue", 0)
    flags = sd_enum_str(chunk, "Flags", "D3D12_FENCE_FLAG_NONE")
    ctx.emit(
        f"  HR(device->CreateFence({initial}, {enum_token(flags)}, IID_PPV_ARGS(&{name})));"
    )


def _emit_create_view_common(ctx: ExportContext, chunk, method: str) -> None:
    desc = sd_child(chunk, "pDesc")
    dest = sd_child(chunk, "DestDescriptor")
    res = sd_resource(chunk, "pResource")
    # We can't reproduce arbitrary CPU descriptor handles without a heap layout
    # tracker — emit a clear stub.
    res_arg = use_resource(ctx, res) if res is not None else "nullptr"
    ctx.emit(
        f"  /* TODO {method}: dest is a CPU descriptor handle into a registered heap. "
        f"Resource={res_arg} */"
    )


@emitter("ID3D12Device::CreateConstantBufferView")
def emit_create_cbv(ctx, chunk):
    _emit_create_view_common(ctx, chunk, "CreateConstantBufferView")


@emitter("ID3D12Device::CreateShaderResourceView")
def emit_create_srv(ctx, chunk):
    _emit_create_view_common(ctx, chunk, "CreateShaderResourceView")


@emitter("ID3D12Device::CreateUnorderedAccessView")
def emit_create_uav(ctx, chunk):
    _emit_create_view_common(ctx, chunk, "CreateUnorderedAccessView")


@emitter("ID3D12Device::CreateRenderTargetView")
def emit_create_rtv(ctx, chunk):
    _emit_create_view_common(ctx, chunk, "CreateRenderTargetView")


@emitter("ID3D12Device::CreateDepthStencilView")
def emit_create_dsv(ctx, chunk):
    _emit_create_view_common(ctx, chunk, "CreateDepthStencilView")


@emitter("ID3D12Device::CreateSampler")
@emitter("ID3D12Device11::CreateSampler2")
def emit_create_sampler(ctx, chunk):
    _emit_create_view_common(ctx, chunk, "CreateSampler")


@emitter("ID3D12Device::CopyDescriptors")
def emit_copy_descriptors(ctx: ExportContext, chunk) -> None:
    ctx.emit(f"  /* TODO CopyDescriptors: multi-range copy, see DescriptorCopies array */")


@emitter("ID3D12Device::CopyDescriptorsSimple")
def emit_copy_descriptors_simple(ctx: ExportContext, chunk) -> None:
    ctx.emit(
        f"  /* TODO CopyDescriptorsSimple: dst/src PortableHandles, see DescriptorCopies array */"
    )


# ---------------------------------------------------------------------------
# Command list chunk emitters
# ---------------------------------------------------------------------------

def cmdlist_var(ctx: ExportContext, chunk) -> str:
    """Return the C++ variable name for the command list that owns ``chunk``.

    Chunks like ``List_Close`` and ``List_Reset`` don't have an explicit
    ``pCommandList`` arg — they're serialised against the wrapped command
    list's record. We fall back to tracking the most recent command list
    seen in any chunk; this works for single-threaded command list recording,
    which is the common case.
    """
    rid = sd_resource(chunk, "pCommandList")
    if rid is None or str(rid) in ("ResourceId()", "0"):
        # Close / Reset don't include an explicit pCommandList — the chunk
        # serialises a BakedCommandList that's RenderDoc-internal and not
        # something we can construct in the exported code. Fall back to the
        # most recently used command list, which works for single-threaded
        # command list recording (the common case).
        rid_str = getattr(ctx, "_last_cmdlist", None)
        if rid_str:
            name = ctx.names.lookup_by_str(rid_str)
            if name:
                return f"{name}.Get()"
        return "/* command list unresolved */ nullptr"
    # Remember the most recent known-good command list reference for the
    # Close/Reset fallback above.
    if ctx.names.has(rid):
        ctx._last_cmdlist = str(rid)
    return use_resource(ctx, rid)


@emitter("ID3D12GraphicsCommandList::Close")
def emit_list_close(ctx, chunk):
    ctx.emit(f"  HR({cmdlist_var(ctx, chunk)}->Close());")


@emitter("ID3D12GraphicsCommandList::Reset")
def emit_list_reset(ctx, chunk):
    alloc = sd_resource(chunk, "pAllocator")
    pso = sd_resource(chunk, "pInitialState")
    ctx.emit(
        f"  HR({cmdlist_var(ctx, chunk)}->Reset({use_resource(ctx, alloc)}, "
        f"{use_resource(ctx, pso)}));"
    )


@emitter("ID3D12GraphicsCommandList::ResourceBarrier")
def emit_resource_barrier(ctx, chunk):
    ctx.emit(f"  /* TODO ResourceBarrier: walk Barriers array */")


@emitter("ID3D12GraphicsCommandList::SetPipelineState")
def emit_set_pso(ctx, chunk):
    pso = sd_resource(chunk, "pPipelineState")
    ctx.emit(f"  {cmdlist_var(ctx, chunk)}->SetPipelineState({use_resource(ctx, pso)});")


@emitter("ID3D12GraphicsCommandList::SetGraphicsRootSignature")
def emit_set_gfx_rs(ctx, chunk):
    rs = sd_resource(chunk, "pRootSignature")
    ctx.emit(f"  {cmdlist_var(ctx, chunk)}->SetGraphicsRootSignature({use_resource(ctx, rs)});")


@emitter("ID3D12GraphicsCommandList::SetComputeRootSignature")
def emit_set_compute_rs(ctx, chunk):
    rs = sd_resource(chunk, "pRootSignature")
    ctx.emit(f"  {cmdlist_var(ctx, chunk)}->SetComputeRootSignature({use_resource(ctx, rs)});")


@emitter("ID3D12GraphicsCommandList::SetDescriptorHeaps")
def emit_set_desc_heaps(ctx, chunk):
    heaps_arr = sd_child(chunk, "ppDescriptorHeaps")
    handles = []
    if heaps_arr is not None:
        for i in range(heaps_arr.NumChildren()):
            rid = heaps_arr.GetChild(i).AsResourceId() if sd_is_resource(heaps_arr.GetChild(i)) else None
            handles.append(use_resource(ctx, rid))
    if not handles:
        ctx.emit(f"  /* TODO SetDescriptorHeaps: empty/unknown array */")
        return
    arr_lit = ", ".join(handles)
    ctx.emit(f"  {{ ID3D12DescriptorHeap *heaps[] = {{ {arr_lit} }};")
    ctx.emit(
        f"    {cmdlist_var(ctx, chunk)}->SetDescriptorHeaps((UINT)std::size(heaps), heaps); }}"
    )


def _emit_set_root_table(ctx, chunk, method: str):
    cv = cmdlist_var(ctx, chunk)
    idx = sd_uint(chunk, "RootParameterIndex", 0)
    # DescriptorTable arg is a GPU handle; emit a placeholder until we have a
    # GPU descriptor handle tracker.
    ctx.emit(f"  {cv}->{method}({idx}, /* TODO GPU descriptor handle */ {{0}});")


@emitter("ID3D12GraphicsCommandList::SetGraphicsRootDescriptorTable")
def emit_set_gfx_root_table(ctx, chunk):
    _emit_set_root_table(ctx, chunk, "SetGraphicsRootDescriptorTable")


@emitter("ID3D12GraphicsCommandList::SetComputeRootDescriptorTable")
def emit_set_compute_root_table(ctx, chunk):
    _emit_set_root_table(ctx, chunk, "SetComputeRootDescriptorTable")


def _emit_set_root_constants(ctx, chunk, method: str, is_array: bool):
    cv = cmdlist_var(ctx, chunk)
    idx = sd_uint(chunk, "RootParameterIndex", 0)
    if is_array:
        num = sd_uint(chunk, "Num32BitValuesToSet", 0)
        offset = sd_uint(chunk, "DestOffsetIn32BitValues", 0)
        ctx.emit(
            f"  /* TODO {method}: {num} constants from chunk at root param {idx} (offset {offset}) */"
        )
    else:
        val = sd_uint(chunk, "SrcData", 0)
        offset = sd_uint(chunk, "DestOffsetIn32BitValues", 0)
        ctx.emit(f"  {cv}->{method}({idx}, {val}u, {offset});")


@emitter("ID3D12GraphicsCommandList::SetGraphicsRoot32BitConstant")
def emit_set_gfx_root_const(ctx, chunk):
    _emit_set_root_constants(ctx, chunk, "SetGraphicsRoot32BitConstant", is_array=False)


@emitter("ID3D12GraphicsCommandList::SetGraphicsRoot32BitConstants")
def emit_set_gfx_root_consts(ctx, chunk):
    _emit_set_root_constants(ctx, chunk, "SetGraphicsRoot32BitConstants", is_array=True)


@emitter("ID3D12GraphicsCommandList::SetComputeRoot32BitConstant")
def emit_set_compute_root_const(ctx, chunk):
    _emit_set_root_constants(ctx, chunk, "SetComputeRoot32BitConstant", is_array=False)


@emitter("ID3D12GraphicsCommandList::SetComputeRoot32BitConstants")
def emit_set_compute_root_consts(ctx, chunk):
    _emit_set_root_constants(ctx, chunk, "SetComputeRoot32BitConstants", is_array=True)


def _emit_set_root_view(ctx, chunk, method: str):
    cv = cmdlist_var(ctx, chunk)
    idx = sd_uint(chunk, "RootParameterIndex", 0)
    addr = sd_uint(chunk, "BufferLocation", 0)
    ctx.emit(f"  {cv}->{method}({idx}, {addr}ull /* GPU VA */);")


@emitter("ID3D12GraphicsCommandList::SetGraphicsRootConstantBufferView")
def emit_set_gfx_root_cbv(ctx, chunk):
    _emit_set_root_view(ctx, chunk, "SetGraphicsRootConstantBufferView")


@emitter("ID3D12GraphicsCommandList::SetGraphicsRootShaderResourceView")
def emit_set_gfx_root_srv(ctx, chunk):
    _emit_set_root_view(ctx, chunk, "SetGraphicsRootShaderResourceView")


@emitter("ID3D12GraphicsCommandList::SetGraphicsRootUnorderedAccessView")
def emit_set_gfx_root_uav(ctx, chunk):
    _emit_set_root_view(ctx, chunk, "SetGraphicsRootUnorderedAccessView")


@emitter("ID3D12GraphicsCommandList::SetComputeRootConstantBufferView")
def emit_set_compute_root_cbv(ctx, chunk):
    _emit_set_root_view(ctx, chunk, "SetComputeRootConstantBufferView")


@emitter("ID3D12GraphicsCommandList::SetComputeRootShaderResourceView")
def emit_set_compute_root_srv(ctx, chunk):
    _emit_set_root_view(ctx, chunk, "SetComputeRootShaderResourceView")


@emitter("ID3D12GraphicsCommandList::SetComputeRootUnorderedAccessView")
def emit_set_compute_root_uav(ctx, chunk):
    _emit_set_root_view(ctx, chunk, "SetComputeRootUnorderedAccessView")


@emitter("ID3D12GraphicsCommandList::IASetPrimitiveTopology")
def emit_ia_set_topo(ctx, chunk):
    topo = sd_enum_str(chunk, "PrimitiveTopology", "D3D_PRIMITIVE_TOPOLOGY_UNDEFINED")
    ctx.emit(f"  {cmdlist_var(ctx, chunk)}->IASetPrimitiveTopology({enum_token(topo)});")


@emitter("ID3D12GraphicsCommandList::IASetIndexBuffer")
def emit_ia_set_ib(ctx, chunk):
    view = sd_child(chunk, "pView")
    if view is None:
        ctx.emit(f"  {cmdlist_var(ctx, chunk)}->IASetIndexBuffer(nullptr);")
        return
    addr = sd_uint(view, "BufferLocation", 0)
    size = sd_uint(view, "SizeInBytes", 0)
    fmt = sd_enum_str(view, "Format", "DXGI_FORMAT_R16_UINT")
    ctx.emit(
        f"  {{ D3D12_INDEX_BUFFER_VIEW v = {{ {addr}ull, {size}, {enum_token(fmt)} }}; "
        f"{cmdlist_var(ctx, chunk)}->IASetIndexBuffer(&v); }}"
    )


@emitter("ID3D12GraphicsCommandList::IASetVertexBuffers")
def emit_ia_set_vb(ctx, chunk):
    start = sd_uint(chunk, "StartSlot", 0)
    views = sd_child(chunk, "pViews")
    if views is None or views.NumChildren() == 0:
        ctx.emit(f"  {cmdlist_var(ctx, chunk)}->IASetVertexBuffers({start}, 0, nullptr);")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_VERTEX_BUFFER_VIEW vbs[{views.NumChildren()}] = {{}};")
    for i in range(views.NumChildren()):
        v = views.GetChild(i)
        addr = sd_uint(v, "BufferLocation", 0)
        size = sd_uint(v, "SizeInBytes", 0)
        stride = sd_uint(v, "StrideInBytes", 0)
        ctx.emit(
            f"    vbs[{i}] = D3D12_VERTEX_BUFFER_VIEW{{ {addr}ull, {size}, {stride} }};"
        )
    ctx.emit(
        f"    {cmdlist_var(ctx, chunk)}->IASetVertexBuffers({start}, {views.NumChildren()}, vbs);"
    )
    ctx.emit(f"  }}")


@emitter("ID3D12GraphicsCommandList::OMSetRenderTargets")
def emit_om_set_rts(ctx, chunk):
    ctx.emit(
        f"  /* TODO OMSetRenderTargets: read pRenderTargetDescriptors + pDepthStencilDescriptor */"
    )


@emitter("ID3D12GraphicsCommandList::OMSetBlendFactor")
def emit_om_set_blend(ctx, chunk):
    arr = sd_child(chunk, "BlendFactor")
    if arr is None or arr.NumChildren() < 4:
        ctx.emit(f"  {{ FLOAT bf[4] = {{1,1,1,1}}; {cmdlist_var(ctx, chunk)}->OMSetBlendFactor(bf); }}")
        return
    vals = [cpp_float(sd_value(arr.GetChild(i)) or 0.0) for i in range(4)]
    ctx.emit(f"  {{ FLOAT bf[4] = {{ {', '.join(vals)} }}; ")
    ctx.emit(f"    {cmdlist_var(ctx, chunk)}->OMSetBlendFactor(bf); }}")


@emitter("ID3D12GraphicsCommandList::OMSetStencilRef")
def emit_om_set_stencil(ctx, chunk):
    r = sd_uint(chunk, "StencilRef", 0)
    ctx.emit(f"  {cmdlist_var(ctx, chunk)}->OMSetStencilRef({r});")


@emitter("ID3D12GraphicsCommandList::RSSetViewports")
def emit_rs_set_viewports(ctx, chunk):
    vps = sd_child(chunk, "pViewports")
    if vps is None or vps.NumChildren() == 0:
        ctx.emit(f"  {cmdlist_var(ctx, chunk)}->RSSetViewports(0, nullptr);")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_VIEWPORT vps[{vps.NumChildren()}] = {{}};")
    for i in range(vps.NumChildren()):
        v = vps.GetChild(i)
        x = cpp_float(sd_float(v, "TopLeftX", 0.0))
        y = cpp_float(sd_float(v, "TopLeftY", 0.0))
        w = cpp_float(sd_float(v, "Width", 0.0))
        h = cpp_float(sd_float(v, "Height", 0.0))
        zn = cpp_float(sd_float(v, "MinDepth", 0.0))
        zf = cpp_float(sd_float(v, "MaxDepth", 1.0))
        ctx.emit(f"    vps[{i}] = D3D12_VIEWPORT{{ {x}, {y}, {w}, {h}, {zn}, {zf} }};")
    ctx.emit(f"    {cmdlist_var(ctx, chunk)}->RSSetViewports({vps.NumChildren()}, vps);")
    ctx.emit(f"  }}")


@emitter("ID3D12GraphicsCommandList::RSSetScissorRects")
def emit_rs_set_scissors(ctx, chunk):
    rcs = sd_child(chunk, "pRects")
    if rcs is None or rcs.NumChildren() == 0:
        ctx.emit(f"  {cmdlist_var(ctx, chunk)}->RSSetScissorRects(0, nullptr);")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_RECT rcs[{rcs.NumChildren()}] = {{}};")
    for i in range(rcs.NumChildren()):
        r = rcs.GetChild(i)
        l = sd_int(r, "left", 0)
        t = sd_int(r, "top", 0)
        rr = sd_int(r, "right", 0)
        b = sd_int(r, "bottom", 0)
        ctx.emit(f"    rcs[{i}] = D3D12_RECT{{ {l}, {t}, {rr}, {b} }};")
    ctx.emit(f"    {cmdlist_var(ctx, chunk)}->RSSetScissorRects({rcs.NumChildren()}, rcs);")
    ctx.emit(f"  }}")


@emitter("ID3D12GraphicsCommandList::ClearRenderTargetView")
def emit_clear_rtv(ctx, chunk):
    color = sd_child(chunk, "ColorRGBA")
    if color is None or color.NumChildren() < 4:
        ctx.emit(f"  /* TODO ClearRenderTargetView: missing color */")
        return
    vals = [cpp_float(sd_value(color.GetChild(i)) or 0.0) for i in range(4)]
    ctx.emit(
        f"  {{ FLOAT c[4] = {{ {', '.join(vals)} }};"
        f" {cmdlist_var(ctx, chunk)}->ClearRenderTargetView(/* TODO RTV handle */ {{0}}, c, 0, nullptr); }}"
    )


@emitter("ID3D12GraphicsCommandList::ClearDepthStencilView")
def emit_clear_dsv(ctx, chunk):
    flags = sd_enum_str(chunk, "ClearFlags", "D3D12_CLEAR_FLAG_DEPTH")
    depth = cpp_float(sd_float(chunk, "Depth", 1.0))
    stencil = sd_uint(chunk, "Stencil", 0)
    ctx.emit(
        f"  {cmdlist_var(ctx, chunk)}->ClearDepthStencilView(/* TODO DSV handle */ {{0}}, "
        f"{enum_token(flags)}, {depth}, {stencil}, 0, nullptr);"
    )


@emitter("ID3D12GraphicsCommandList::ClearUnorderedAccessViewUint")
def emit_clear_uav_uint(ctx, chunk):
    ctx.emit(f"  /* TODO ClearUnorderedAccessViewUint: GPU+CPU handles + resource + UINT[4] */")


@emitter("ID3D12GraphicsCommandList::ClearUnorderedAccessViewFloat")
def emit_clear_uav_float(ctx, chunk):
    ctx.emit(f"  /* TODO ClearUnorderedAccessViewFloat: GPU+CPU handles + resource + FLOAT[4] */")


@emitter("ID3D12GraphicsCommandList::CopyResource")
def emit_copy_resource(ctx, chunk):
    dst = sd_resource(chunk, "pDstResource")
    src = sd_resource(chunk, "pSrcResource")
    ctx.emit(
        f"  {cmdlist_var(ctx, chunk)}->CopyResource({use_resource(ctx, dst)}, "
        f"{use_resource(ctx, src)});"
    )


@emitter("ID3D12GraphicsCommandList::CopyBufferRegion")
def emit_copy_buffer_region(ctx, chunk):
    dst = sd_resource(chunk, "pDstBuffer")
    src = sd_resource(chunk, "pSrcBuffer")
    dst_off = sd_uint(chunk, "DstOffset", 0)
    src_off = sd_uint(chunk, "SrcOffset", 0)
    num = sd_uint(chunk, "NumBytes", 0)
    ctx.emit(
        f"  {cmdlist_var(ctx, chunk)}->CopyBufferRegion({use_resource(ctx, dst)}, {dst_off}, "
        f"{use_resource(ctx, src)}, {src_off}, {num});"
    )


@emitter("ID3D12GraphicsCommandList::CopyTextureRegion")
def emit_copy_texture_region(ctx, chunk):
    ctx.emit(
        f"  /* TODO CopyTextureRegion: needs D3D12_TEXTURE_COPY_LOCATION dst/src reconstruction */"
    )


@emitter("ID3D12GraphicsCommandList::ResolveSubresource")
def emit_resolve_sub(ctx, chunk):
    dst = sd_resource(chunk, "pDstResource")
    src = sd_resource(chunk, "pSrcResource")
    dst_sub = sd_uint(chunk, "DstSubresource", 0)
    src_sub = sd_uint(chunk, "SrcSubresource", 0)
    fmt = sd_enum_str(chunk, "Format", "DXGI_FORMAT_UNKNOWN")
    ctx.emit(
        f"  {cmdlist_var(ctx, chunk)}->ResolveSubresource({use_resource(ctx, dst)}, {dst_sub}, "
        f"{use_resource(ctx, src)}, {src_sub}, {enum_token(fmt)});"
    )


@emitter("ID3D12GraphicsCommandList::DiscardResource")
def emit_discard_resource(ctx, chunk):
    res = sd_resource(chunk, "pResource")
    ctx.emit(
        f"  {cmdlist_var(ctx, chunk)}->DiscardResource({use_resource(ctx, res)}, nullptr);"
    )


@emitter("ID3D12GraphicsCommandList::DrawInstanced")
def emit_draw_instanced(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    vpe = sd_uint(chunk, "VertexCountPerInstance", 0)
    inst = sd_uint(chunk, "InstanceCount", 1)
    sv = sd_uint(chunk, "StartVertexLocation", 0)
    si = sd_uint(chunk, "StartInstanceLocation", 0)
    ctx.emit(f"  {cv}->DrawInstanced({vpe}, {inst}, {sv}, {si});")


@emitter("ID3D12GraphicsCommandList::DrawIndexedInstanced")
def emit_draw_indexed(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    ipe = sd_uint(chunk, "IndexCountPerInstance", 0)
    inst = sd_uint(chunk, "InstanceCount", 1)
    si = sd_uint(chunk, "StartIndexLocation", 0)
    bvl = sd_int(chunk, "BaseVertexLocation", 0)
    sil = sd_uint(chunk, "StartInstanceLocation", 0)
    ctx.emit(f"  {cv}->DrawIndexedInstanced({ipe}, {inst}, {si}, {bvl}, {sil});")


@emitter("ID3D12GraphicsCommandList::Dispatch")
def emit_dispatch(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    x = sd_uint(chunk, "ThreadGroupCountX", 1)
    y = sd_uint(chunk, "ThreadGroupCountY", 1)
    z = sd_uint(chunk, "ThreadGroupCountZ", 1)
    ctx.emit(f"  {cv}->Dispatch({x}, {y}, {z});")


@emitter("ID3D12GraphicsCommandList::ExecuteIndirect")
def emit_execute_indirect(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    sig = sd_resource(chunk, "pCommandSignature")
    max_count = sd_uint(chunk, "MaxCommandCount", 0)
    args = sd_resource(chunk, "pArgumentBuffer")
    args_off = sd_uint(chunk, "ArgumentBufferOffset", 0)
    count = sd_resource(chunk, "pCountBuffer")
    count_off = sd_uint(chunk, "CountBufferOffset", 0)
    ctx.emit(
        f"  {cv}->ExecuteIndirect({use_resource(ctx, sig)}, {max_count}, "
        f"{use_resource(ctx, args)}, {args_off}, {use_resource(ctx, count)}, {count_off});"
    )


@emitter("ID3D12GraphicsCommandList6::DispatchMesh")
def emit_dispatch_mesh(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    x = sd_uint(chunk, "ThreadGroupCountX", 1)
    y = sd_uint(chunk, "ThreadGroupCountY", 1)
    z = sd_uint(chunk, "ThreadGroupCountZ", 1)
    ctx.emit(
        f"  {{ ComPtr<ID3D12GraphicsCommandList6> cl6; HR(({cv})->QueryInterface(IID_PPV_ARGS(&cl6)));"
        f" cl6->DispatchMesh({x}, {y}, {z}); }}"
    )


@emitter("ID3D12GraphicsCommandList4::DispatchRays")
def emit_dispatch_rays(ctx, chunk):
    ctx.emit(f"  /* TODO DispatchRays: requires D3D12_DISPATCH_RAYS_DESC reconstruction */")


# ---------------------------------------------------------------------------
# Queue + marker emitters
# ---------------------------------------------------------------------------

@emitter("ID3D12CommandQueue::ExecuteCommandLists")
def emit_queue_execute(ctx, chunk):
    qv = use_resource(ctx, sd_resource(chunk, "pQueue"))
    lists = sd_child(chunk, "ppCommandLists")
    if lists is None or lists.NumChildren() == 0:
        ctx.emit(f"  /* TODO ExecuteCommandLists: empty list array */")
        return
    handles = []
    for i in range(lists.NumChildren()):
        c = lists.GetChild(i)
        rid = c.AsResourceId() if sd_is_resource(c) else None
        handles.append(use_resource(ctx, rid))
    arr = ", ".join(handles)
    ctx.emit(f"  {{ ID3D12CommandList *lists[] = {{ {arr} }}; ")
    ctx.emit(f"    {qv}->ExecuteCommandLists((UINT)std::size(lists), lists); }}")


@emitter("ID3D12CommandQueue::Signal")
def emit_queue_signal(ctx, chunk):
    qv = use_resource(ctx, sd_resource(chunk, "pQueue"))
    fence = sd_resource(chunk, "pFence")
    value = sd_uint(chunk, "Value", 0)
    ctx.emit(f"  HR({qv}->Signal({use_resource(ctx, fence)}, {value}ull));")


@emitter("ID3D12CommandQueue::Wait")
def emit_queue_wait(ctx, chunk):
    qv = use_resource(ctx, sd_resource(chunk, "pQueue"))
    fence = sd_resource(chunk, "pFence")
    value = sd_uint(chunk, "Value", 0)
    ctx.emit(f"  HR({qv}->Wait({use_resource(ctx, fence)}, {value}ull));")


@emitter("PushMarker")
@emitter("SetMarker")
@emitter("PopMarker")
def emit_marker(ctx, chunk):
    name = sd_value(sd_child(chunk, "Name")) or ""
    ctx.emit(f"  /* marker [{chunk.name}]: {name} */")


# ---------------------------------------------------------------------------
# Additional emitters for less common chunks
# ---------------------------------------------------------------------------

@emitter("ID3D12Object::SetName")
@emitter("ID3D12Resource::SetName")
def emit_set_name(ctx, chunk):
    name_val = sd_value(sd_child(chunk, "Name")) or ""
    rid = sd_resource(chunk, "pObject")
    if rid is None:
        # SetName chunks sometimes record the object as "this" using a
        # parented name like "pResource". Fall back to scanning for a
        # ResourceId child.
        try:
            for i in range(chunk.NumChildren()):
                c = chunk.GetChild(i)
                if sd_is_resource(c):
                    try:
                        rid = c.AsResourceId()
                        break
                    except Exception:
                        pass
        except Exception:
            pass
    target = use_resource(ctx, rid) if rid is not None else "/* unknown */ nullptr"
    safe = cpp_string(str(name_val))
    if "nullptr" in target:
        ctx.emit(f"  /* SetName({safe}) - target ResourceId unresolved */")
    else:
        ctx.emit(f"  if({target}) {target}->SetPrivateData(WKPDID_D3DDebugObjectNameW, "
                 f"(UINT)wcslen(L{safe}) * 2, L{safe});")


@emitter("ID3D12Device::CreateQueryHeap")
def emit_create_query_heap(ctx, chunk):
    rid = sd_resource(chunk, "pQueryHeap")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "QueryHeap", "ID3D12QueryHeap")
    desc = sd_child(chunk, "pDesc")
    typ = sd_enum_str(desc, "Type", "D3D12_QUERY_HEAP_TYPE_OCCLUSION")
    count = sd_uint(desc, "Count", 1)
    node_mask = sd_uint(desc, "NodeMask", 0)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_QUERY_HEAP_DESC d = {{}};")
    ctx.emit(f"    d.Type = {enum_token(typ)};")
    ctx.emit(f"    d.Count = {count};")
    ctx.emit(f"    d.NodeMask = {node_mask};")
    ctx.emit(f"    HR(device->CreateQueryHeap(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12GraphicsCommandList::BeginQuery")
def emit_begin_query(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    qh = use_resource(ctx, sd_resource(chunk, "pQueryHeap"))
    typ = sd_enum_str(chunk, "Type", "D3D12_QUERY_TYPE_OCCLUSION")
    idx = sd_uint(chunk, "Index", 0)
    ctx.emit(f"  {cv}->BeginQuery({qh}, {enum_token(typ)}, {idx});")


@emitter("ID3D12GraphicsCommandList::EndQuery")
def emit_end_query(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    qh = use_resource(ctx, sd_resource(chunk, "pQueryHeap"))
    typ = sd_enum_str(chunk, "Type", "D3D12_QUERY_TYPE_OCCLUSION")
    idx = sd_uint(chunk, "Index", 0)
    ctx.emit(f"  {cv}->EndQuery({qh}, {enum_token(typ)}, {idx});")


@emitter("ID3D12GraphicsCommandList::ResolveQueryData")
def emit_resolve_query_data(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    qh = use_resource(ctx, sd_resource(chunk, "pQueryHeap"))
    typ = sd_enum_str(chunk, "Type", "D3D12_QUERY_TYPE_OCCLUSION")
    start = sd_uint(chunk, "StartIndex", 0)
    count = sd_uint(chunk, "NumQueries", 1)
    dst = use_resource(ctx, sd_resource(chunk, "pDestinationBuffer"))
    dst_off = sd_uint(chunk, "AlignedDestinationBufferOffset", 0)
    ctx.emit(
        f"  {cv}->ResolveQueryData({qh}, {enum_token(typ)}, {start}, {count}, {dst}, {dst_off});"
    )


@emitter("IDXGISwapChain::GetBuffer")
def emit_swapchain_get_buffer(ctx, chunk):
    # The chunk records the index of the backbuffer + the ResourceId it was
    # assigned. We treat it like an opaque resource creation so subsequent
    # SetRenderTargets etc. can reference it.
    rid = None
    try:
        for i in range(chunk.NumChildren()):
            c = chunk.GetChild(i)
            if sd_is_resource(c) and str(c.name).lower().startswith("pp"):
                rid = c.AsResourceId()
                break
    except Exception:
        pass
    if rid is None:
        rid = sd_resource(chunk, "ppSurface")
    if rid is None:
        rid = sd_resource(chunk, "pBuffer")
    if rid is None:
        return
    idx = sd_uint(chunk, "Buffer", 0)
    name = declare_resource(ctx, rid, "Texture2D", "ID3D12Resource")
    ctx.emit(f"  /* TODO IDXGISwapChain::GetBuffer({idx}) -> {name} (provide your own swap chain) */")


@emitter("IDXGISwapChain::Present")
@emitter("IDXGISwapChain1::Present1")
def emit_swapchain_present(ctx, chunk):
    interval = sd_uint(chunk, "SyncInterval", 1)
    flags = sd_uint(chunk, "Flags", 0)
    ctx.emit(f"  /* Frame boundary: Present(SyncInterval={interval}, Flags={flags}) */")


@emitter("Internal::Beginning of Capture")
@emitter("Internal::End of Capture")
@emitter("Internal::Frame Metadata")
@emitter("Internal::Driver Initialisation Parameters")
@emitter("Internal::List of Initial Contents Resources")
@emitter("Internal::Initial Contents")
@emitter("Internal::Coherent Mapped Memory Write")
def emit_internal_chunk(ctx, chunk):
    # Internal RenderDoc chunks aren't real D3D12 API calls — skip them
    # without polluting unhandled.txt.
    ctx.emit(f"  // (internal RenderDoc chunk skipped: {chunk.name})")


@emitter("ID3D12GraphicsCommandList::ClearState")
def emit_clear_state(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    pso = sd_resource(chunk, "pPipelineState")
    ctx.emit(f"  {cv}->ClearState({use_resource(ctx, pso)});")


@emitter("ID3D12GraphicsCommandList::SOSetTargets")
def emit_so_set_targets(ctx, chunk):
    ctx.emit(f"  /* TODO SOSetTargets: walk pViews array of D3D12_STREAM_OUTPUT_BUFFER_VIEW */")


@emitter("ID3D12GraphicsCommandList1::OMSetDepthBounds")
def emit_om_depth_bounds(ctx, chunk):
    mn = cpp_float(sd_float(chunk, "Min", 0.0))
    mx = cpp_float(sd_float(chunk, "Max", 1.0))
    ctx.emit(
        f"  {{ ComPtr<ID3D12GraphicsCommandList1> cl1; HR(({cmdlist_var(ctx, chunk)})->QueryInterface(IID_PPV_ARGS(&cl1)));"
        f" cl1->OMSetDepthBounds({mn}, {mx}); }}"
    )


@emitter("ID3D12GraphicsCommandList5::RSSetShadingRate")
def emit_rs_shading_rate(ctx, chunk):
    rate = sd_enum_str(chunk, "baseShadingRate", "D3D12_SHADING_RATE_1X1")
    ctx.emit(
        f"  {{ ComPtr<ID3D12GraphicsCommandList5> cl5; HR(({cmdlist_var(ctx, chunk)})->QueryInterface(IID_PPV_ARGS(&cl5)));"
        f" cl5->RSSetShadingRate({enum_token(rate)}, nullptr); }}"
    )


@emitter("ID3D12GraphicsCommandList5::RSSetShadingRateImage")
def emit_rs_shading_rate_image(ctx, chunk):
    img = use_resource(ctx, sd_resource(chunk, "shadingRateImage"))
    ctx.emit(
        f"  {{ ComPtr<ID3D12GraphicsCommandList5> cl5; HR(({cmdlist_var(ctx, chunk)})->QueryInterface(IID_PPV_ARGS(&cl5)));"
        f" cl5->RSSetShadingRateImage({img}); }}"
    )


@emitter("ID3D12CommandQueue::BeginEvent")
def emit_queue_begin_event(ctx, chunk):
    ctx.emit(f"  /* queue BeginEvent */")


@emitter("ID3D12CommandQueue::EndEvent")
def emit_queue_end_event(ctx, chunk):
    ctx.emit(f"  /* queue EndEvent */")


@emitter("ID3D12CommandQueue::SetMarker")
def emit_queue_set_marker(ctx, chunk):
    ctx.emit(f"  /* queue SetMarker */")


# ---------------------------------------------------------------------------
# Main exporter
# ---------------------------------------------------------------------------

CAPTURE_FRAME_HEADER = r"""// Auto-generated by util/automation/export_cpp.py
// Reproduces the D3D12 call sequence recorded in the source .rdc capture.

#pragma once

#include <wrl/client.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iterator>

using Microsoft::WRL::ComPtr;

#define HR(x) do { HRESULT _hr = (x); if(FAILED(_hr)) { \
    fprintf(stderr, "HR fail at %s:%d : 0x%08X\n", __FILE__, __LINE__, _hr); std::abort(); } \
} while(0)

extern ComPtr<ID3D12Device> device;

void RecordCapture(ID3D12Device *dev);

"""

MAIN_CPP = r"""// Auto-generated by util/automation/export_cpp.py

#include "capture_frame.h"
#include <cstdio>
#include <cstdlib>
#include <dxgi.h>

ComPtr<ID3D12Device> device;

int main(int argc, char **argv) {
    ComPtr<IDXGIFactory4> factory;
    HR(CreateDXGIFactory1(IID_PPV_ARGS(&factory)));

    ComPtr<IDXGIAdapter1> adapter;
    HR(factory->EnumAdapters1(0, &adapter));

    HR(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)));

    RecordCapture(device.Get());

    fprintf(stdout, "RecordCapture completed.\n");
    return 0;
}
"""

CMAKE = r"""# Auto-generated by util/automation/export_cpp.py
cmake_minimum_required(VERSION 3.20)
project(exported_capture CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(exported_capture main.cpp capture_frame.cpp capture_frame.h)
target_link_libraries(exported_capture PRIVATE d3d12 dxgi dxguid)
target_compile_definitions(exported_capture PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
"""

README = """# Exported D3D12 capture

This is a stripped-down C++ project auto-generated from a RenderDoc capture.

## Building

```sh
cmake -B build -S .
cmake --build build --config Release
```

## What's covered

Most chunks that participate in the per-frame draw/dispatch flow:

  - Device-level creation (CommandQueue/Allocator/List, DescriptorHeap,
    RootSignature, PSOs, CommittedResource/Heap/Fence)
  - Command list recording (set state, draws, dispatches, clears, copies)
  - Queue execution (ExecuteCommandLists, Signal, Wait)

## What's stubbed

Anything that requires CPU/GPU descriptor handle tracking or large external
data is emitted as `/* TODO ... */` comments. The list of skipped chunks is
in `unhandled.txt` (one line per skipped chunk).

Common stubs:

  - `CreateConstantBufferView` / `CreateShaderResourceView` / `CreateUnorderedAccessView`
    / `CreateRenderTargetView` / `CreateDepthStencilView` / `CreateSampler` —
    require a CPU descriptor handle tracker
  - `SetGraphicsRootDescriptorTable` / `SetComputeRootDescriptorTable` —
    require a GPU descriptor handle tracker
  - `OMSetRenderTargets`, `Clear{RenderTarget,DepthStencil}View` —
    require CPU descriptor handles
  - `CopyTextureRegion`, `ResourceBarrier` (array form) —
    require nested-struct reconstruction from the chunk
  - Pipeline state stream PSOs (CreatePipelineState) — require subobject parser
  - Raytracing (`DispatchRays`, `BuildRaytracingAccelerationStructure`) — TBD
  - Root signature blobs — need to be saved as separate `.cso` files (see
    `shaders/`) and loaded at runtime

## Shaders + blobs

`shaders/<hash>.cso` files are the DXBC/DXIL bytecode referenced by PSOs.
`blobs/buf_<id>.bin` files contain initial buffer contents.
"""


def _walk_chunks(controller, ctx: ExportContext) -> None:
    sdfile = controller.GetStructuredFile()
    n = sdfile.chunks.size() if hasattr(sdfile.chunks, "size") else len(sdfile.chunks)
    for i in range(n):
        chunk = sdfile.chunks[i]
        name = str(chunk.name)

        # Event range filter (chunks don't all have an eventId, but markers do)
        if ctx.first_event is not None or ctx.last_event is not None:
            try:
                eid = int(chunk.metadata.eventId)
            except Exception:
                eid = -1
            if ctx.first_event is not None and eid >= 0 and eid < ctx.first_event:
                continue
            if ctx.last_event is not None and eid >= 0 and eid > ctx.last_event:
                continue

        fn = EMITTERS.get(name)
        if fn is None:
            # Unknown chunk — record and continue
            ctx.unhandled_chunk(i, name)
            continue
        try:
            ctx.emit(f"  // chunk #{i}: {name}")
            fn(ctx, chunk)
        except Exception as exc:
            ctx.emit(f"  /* emitter failed for chunk #{i} ({name}): {exc} */")
            ctx.unhandled.append((i, f"{name} (exception: {exc})"))
        ctx._chunks_emitted += 1


def _write_output(ctx: ExportContext) -> None:
    # capture_frame.h
    decls = "\n".join(ctx.decls)
    with open(os.path.join(ctx.out_dir, "capture_frame.h"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CAPTURE_FRAME_HEADER)
        f.write(decls)
        f.write("\n")

    # capture_frame.cpp
    with open(os.path.join(ctx.out_dir, "capture_frame.cpp"), "w", encoding="utf-8", newline="\n") as f:
        f.write("// Auto-generated by util/automation/export_cpp.py\n\n")
        f.write("#include \"capture_frame.h\"\n\n")
        # Resource variable definitions (out-of-line storage).
        for decl in ctx.decls:
            # Convert `extern ComPtr<T> name;` into `ComPtr<T> name;`
            if decl.startswith("extern "):
                f.write(decl[len("extern "):] + "\n")
        f.write("\nvoid RecordCapture(ID3D12Device *dev) {\n")
        f.write("  ComPtr<ID3D12Device> device; device.Attach(dev); dev->AddRef();\n")
        for line in ctx.lines:
            f.write(line + "\n")
        f.write("}\n")

    with open(os.path.join(ctx.out_dir, "main.cpp"), "w", encoding="utf-8", newline="\n") as f:
        f.write(MAIN_CPP)
    with open(os.path.join(ctx.out_dir, "CMakeLists.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CMAKE)
    with open(os.path.join(ctx.out_dir, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(README)

    # unhandled.txt
    with open(os.path.join(ctx.out_dir, "unhandled.txt"), "w", encoding="utf-8", newline="\n") as f:
        for idx, name in ctx.unhandled:
            f.write(f"#{idx}\t{name}\n")


def _extract_shader_blobs(controller, ctx: ExportContext) -> None:
    """Walk every shader reflection in the capture, write its raw bytecode to
    ``shaders/<md5>.cso``. PSOs in the emitted code reference shaders by hash.
    """
    if not ctx.write_blobs:
        return
    seen: set = set()
    for action in _lib.walk_actions(controller):
        if not (
            int(action.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))
        ):
            continue
        try:
            controller.SetFrameEvent(int(action.eventId), True)
        except Exception:
            continue
        pipe = controller.GetPipelineState()
        for stage in (
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
                refl = pipe.GetShaderReflection(stage)
            except Exception:
                refl = None
            if refl is None or len(refl.rawBytes) == 0:
                continue
            raw = bytes(refl.rawBytes)
            h = _lib.shader_bytecode_hash(raw)
            if h in seen:
                continue
            seen.add(h)
            path = os.path.join(ctx.out_dir, "shaders", f"{h}.cso")
            with open(path, "wb") as f:
                f.write(raw)
            ctx.shader_blobs[h] = raw
            ctx.shader_id_to_hash[str(refl.resourceId)] = h


def export(
    capture_path: str,
    out_dir: str,
    first_event: Optional[int] = None,
    last_event: Optional[int] = None,
    write_blobs: bool = True,
) -> Dict[str, Any]:
    """Export ``capture_path`` to a C++ project rooted at ``out_dir``."""
    cap, controller = _lib.open_capture(capture_path)
    try:
        ctx = ExportContext(out_dir, controller=controller)
        ctx.first_event = first_event
        ctx.last_event = last_event
        ctx.write_blobs = write_blobs

        _extract_shader_blobs(controller, ctx)
        _walk_chunks(controller, ctx)
        _write_output(ctx)

        return {
            "out_dir": os.path.abspath(out_dir),
            "chunks_emitted": ctx._chunks_emitted,
            "unhandled_chunks": len(ctx.unhandled),
            "shader_blobs": len(ctx.shader_blobs),
            "resources": len(ctx.names._by_id),
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def export_json(capture_path: str, out_path: str) -> Dict[str, Any]:
    """Dump every SDChunk as JSON — diagnostic mode, no C++."""
    cap, controller = _lib.open_capture(capture_path)
    try:
        sdfile = controller.GetStructuredFile()
        n = sdfile.chunks.size() if hasattr(sdfile.chunks, "size") else len(sdfile.chunks)
        rows = []
        for i in range(n):
            chunk = sdfile.chunks[i]
            try:
                args = sd_to_python(chunk)
            except Exception as exc:
                args = {"error": str(exc)}
            rows.append({"index": i, "name": str(chunk.name), "args": args})
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"count": len(rows), "chunks": rows}, f, indent=2)
        return {"out": os.path.abspath(out_path), "count": len(rows)}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Export a D3D12 capture as a compilable C++ project.")
    p.add_argument("capture")
    p.add_argument("--out", "-o", required=True, help="Output directory")
    p.add_argument("--first-event", type=int, default=None)
    p.add_argument("--last-event", type=int, default=None)
    p.add_argument("--no-blobs", action="store_true", help="Skip shader/buffer blob extraction")
    p.add_argument("--json-only", action="store_true", help="Dump SDFile chunks as JSON instead of C++")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.json_only:
            out_path = os.path.join(args.out, "chunks.json") if os.path.isdir(args.out) or args.out.endswith(os.sep) else args.out
            os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
            result = export_json(args.capture, out_path)
        else:
            result = export(
                args.capture,
                args.out,
                first_event=args.first_event,
                last_event=args.last_event,
                write_blobs=not args.no_blobs,
            )
    finally:
        rd.ShutdownReplay()

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
