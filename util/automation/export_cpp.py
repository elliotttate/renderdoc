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
    """Return the most useful Python scalar for an SDObject.

    The Python bindings (see ``qrenderdoc/Code/pyrenderdoc/renderdoc.i``)
    only expose ``AsInt()``, ``AsFloat()``, ``AsString()``, ``AsResourceId()``.
    ``AsInt()`` handles both signed and unsigned. Booleans and raw data go
    through ``obj.data.basic.*`` directly.
    """
    if obj is None:
        return None
    bt = _basetype(obj)
    try:
        if bt == "Resource":
            return ("ResourceId", str(obj.AsResourceId()))
        if bt == "String":
            return str(obj.AsString())
        if bt == "Boolean":
            return bool(obj.data.basic.b)
        if bt == "Float":
            return float(obj.AsFloat())
        if bt in ("SignedInteger", "UnsignedInteger"):
            return int(obj.AsInt())
        if bt == "Enum":
            try:
                return ("Enum", str(obj.AsString()))
            except Exception:
                return ("Enum", int(obj.AsInt()))
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
        return int(c.AsInt()) if c is not None else default
    except Exception:
        return default


def sd_int(obj, name: str, default: int = 0) -> int:
    c = sd_child(obj, name)
    try:
        return int(c.AsInt()) if c is not None else default
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
            return str(int(c.AsInt()))
        except Exception:
            return default


def sd_byte_array(obj) -> Optional[bytes]:
    """Extract bytes from a structured byte array (each child a UINT8 value).

    Returns ``None`` if ``obj`` is missing or contains no children.
    Designed for ``SERIALISE_MEMBER_ARRAY`` fields like
    ``pBlobWithRootSignature`` (root sig blob) and ``pShaderBytecode``
    (PSO shader subobject body).
    """
    if obj is None:
        return None
    try:
        n = obj.NumChildren()
    except Exception:
        return None
    if n == 0:
        return None
    out = bytearray(n)
    for i in range(n):
        try:
            out[i] = int(obj.GetChild(i).AsInt()) & 0xFF
        except Exception:
            out[i] = 0
    return bytes(out)


def read_portable_handle(obj) -> Optional[Tuple[Any, int]]:
    """Return ``(heap_ResourceId, slot)`` from a PortableHandle SDObject, or
    ``None`` if ``obj`` isn't a PortableHandle.

    Every D3D12 CPU/GPU descriptor handle is serialised as a small struct
    ``{ ResourceId heap; uint32 index; }``. We don't try to identify the
    PortableHandle by type name — we just look for two children named
    ``heap`` and ``index``.
    """
    if obj is None:
        return None
    heap = sd_child(obj, "heap")
    idx = sd_child(obj, "index")
    if heap is None or idx is None:
        return None
    try:
        return (heap.AsResourceId(), int(idx.AsInt()))
    except Exception:
        return None


def _register_implicit_heap(ctx: "ExportContext", heap_id) -> Optional[str]:
    """If a chunk references a descriptor heap we never saw created — typically
    because the heap pre-existed the captured frame and RenderDoc serialised
    it via an internal-state chunk we don't fully reconstruct — register a
    placeholder name so the resulting C++ still references *something* and
    the user can stub in a heap creation manually.
    """
    if heap_id is None or str(heap_id) in ("ResourceId()", "0"):
        return None
    if ctx.names.has(heap_id):
        return ctx.names.lookup(heap_id)
    name = ctx.names.assign(heap_id, "DescriptorHeap")
    ctx.add_decl(f"extern ComPtr<ID3D12DescriptorHeap> {name}; "
                 f"// TODO: pre-existing heap {heap_id}; create with the right type/size")
    return name


def cpu_handle_expr(ctx: "ExportContext", obj) -> str:
    """Build the C++ expression for the CPU descriptor handle described by
    ``obj`` (a PortableHandle SDObject)."""
    ph = read_portable_handle(obj)
    if ph is None:
        return "D3D12_CPU_DESCRIPTOR_HANDLE{0}"
    heap_id, slot = ph
    if str(heap_id) in ("ResourceId()", "0"):
        return "D3D12_CPU_DESCRIPTOR_HANDLE{0}"
    name = ctx.names.lookup(heap_id) or _register_implicit_heap(ctx, heap_id)
    if not name:
        return f"D3D12_CPU_DESCRIPTOR_HANDLE{{0}} /* unresolved heap {heap_id} slot {slot} */"
    return f"CpuHandle({name}.Get(), {slot})"


def gpu_handle_expr(ctx: "ExportContext", obj) -> str:
    """Build the C++ expression for the GPU descriptor handle described by
    ``obj`` (a PortableHandle SDObject)."""
    ph = read_portable_handle(obj)
    if ph is None:
        return "D3D12_GPU_DESCRIPTOR_HANDLE{0}"
    heap_id, slot = ph
    if str(heap_id) in ("ResourceId()", "0"):
        return "D3D12_GPU_DESCRIPTOR_HANDLE{0}"
    name = ctx.names.lookup(heap_id) or _register_implicit_heap(ctx, heap_id)
    if not name:
        return f"D3D12_GPU_DESCRIPTOR_HANDLE{{0}} /* unresolved heap {heap_id} slot {slot} */"
    return f"GpuHandle({name}.Get(), {slot})"


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
        # (pso_resource_id_str, stage_field) -> shader-bytecode md5
        # e.g. ("ResourceId::1234", "VS") -> "abcd...md5"
        self.pso_shader_hashes: Dict[Tuple[str, str], str] = {}
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


def use_resource(ctx: ExportContext, rid, *, register_as: Optional[Tuple[str, str]] = None) -> str:
    """Return the C++ variable name for ``rid``, or a placeholder if unknown.

    If ``register_as`` is provided as ``(kind, cpp_type)`` and ``rid`` is
    unknown, register a placeholder declaration so the resulting C++ at
    least references *something* the user can fill in. This is the right
    behaviour for resources we see being used (e.g. via CreateRenderTargetView)
    but never saw being created (typically swap chain back buffers or
    pre-existing heaps).
    """
    if rid is None:
        return "nullptr"
    s = str(rid)
    if s in ("ResourceId()", "0"):
        return "nullptr"
    name = ctx.names.lookup(rid)
    if name:
        return f"{name}.Get()"
    if register_as is not None:
        kind, cpp_type = register_as
        new_name = ctx.names.assign(rid, kind)
        ctx.add_decl(
            f"extern ComPtr<{cpp_type}> {new_name}; "
            f"// TODO: pre-existing {kind} {s} (provide your own creation)"
        )
        return f"{new_name}.Get()"
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
    blob_obj = sd_child(chunk, "pBlobWithRootSignature")
    blob_bytes = sd_byte_array(blob_obj) if ctx.write_blobs else None
    if blob_bytes:
        digest = hashlib.md5(blob_bytes).hexdigest()
        path = os.path.join(ctx.out_dir, "blobs", f"rs_{digest}.bin")
        with open(path, "wb") as f:
            f.write(blob_bytes)
        ctx.buffer_blobs[f"rs_{digest}"] = blob_bytes
        ctx.emit(f"  {{")
        ctx.emit(f"    auto blob = LoadBlob(\"blobs/rs_{digest}.bin\");")
        ctx.emit(
            f"    HR(device->CreateRootSignature({node_mask}, blob.data(), blob.size(), "
            f"IID_PPV_ARGS(&{name})));"
        )
        ctx.emit(f"  }}")
        return
    blob_size = sd_uint(chunk, "blobLengthInBytes", 0)
    ctx.emit(
        f"  /* TODO: provide {blob_size}-byte serialized root signature blob */"
    )
    ctx.emit(
        f"  HR(device->CreateRootSignature({node_mask}, nullptr, {blob_size}, "
        f"IID_PPV_ARGS(&{name})));"
    )


def _emit_blend_state(ctx, obj, var: str, indent: str = "    ") -> None:
    """Populate D3D12_BLEND_DESC at ``var``."""
    if obj is None:
        return
    ctx.emit(f"{indent}{var}.AlphaToCoverageEnable = {1 if sd_value(sd_child(obj, 'AlphaToCoverageEnable')) else 0};")
    ctx.emit(f"{indent}{var}.IndependentBlendEnable = {1 if sd_value(sd_child(obj, 'IndependentBlendEnable')) else 0};")
    rts = sd_child(obj, "RenderTarget")
    if rts is not None:
        for i in range(min(rts.NumChildren(), 8)):
            rt = rts.GetChild(i)
            be = 1 if sd_value(sd_child(rt, "BlendEnable")) else 0
            lo = 1 if sd_value(sd_child(rt, "LogicOpEnable")) else 0
            srcb = sd_enum_str(rt, "SrcBlend", "D3D12_BLEND_ONE")
            dstb = sd_enum_str(rt, "DestBlend", "D3D12_BLEND_ZERO")
            opb = sd_enum_str(rt, "BlendOp", "D3D12_BLEND_OP_ADD")
            srca = sd_enum_str(rt, "SrcBlendAlpha", "D3D12_BLEND_ONE")
            dsta = sd_enum_str(rt, "DestBlendAlpha", "D3D12_BLEND_ZERO")
            opa = sd_enum_str(rt, "BlendOpAlpha", "D3D12_BLEND_OP_ADD")
            logo = sd_enum_str(rt, "LogicOp", "D3D12_LOGIC_OP_NOOP")
            mask = sd_uint(rt, "RenderTargetWriteMask", 0xF)
            ctx.emit(f"{indent}{var}.RenderTarget[{i}] = D3D12_RENDER_TARGET_BLEND_DESC{{ "
                     f"{be}, {lo}, {enum_token(srcb)}, {enum_token(dstb)}, {enum_token(opb)}, "
                     f"{enum_token(srca)}, {enum_token(dsta)}, {enum_token(opa)}, "
                     f"{enum_token(logo)}, (UINT8){mask} }};")


def _emit_rasterizer(ctx, obj, var: str, indent: str = "    ") -> None:
    if obj is None:
        return
    _emit_assigns(ctx, indent, var, obj, [
        ("FillMode", "enum", "D3D12_FILL_MODE_SOLID"),
        ("CullMode", "enum", "D3D12_CULL_MODE_BACK"),
    ])
    fcc = 1 if sd_value(sd_child(obj, "FrontCounterClockwise")) else 0
    ctx.emit(f"{indent}{var}.FrontCounterClockwise = {fcc};")
    _emit_assigns(ctx, indent, var, obj, [
        ("DepthBias", "int"),
        ("DepthBiasClamp", "float"),
        ("SlopeScaledDepthBias", "float"),
    ])
    de = 1 if sd_value(sd_child(obj, "DepthClipEnable")) else 0
    me = 1 if sd_value(sd_child(obj, "MultisampleEnable")) else 0
    aae = 1 if sd_value(sd_child(obj, "AntialiasedLineEnable")) else 0
    ctx.emit(f"{indent}{var}.DepthClipEnable = {de};")
    ctx.emit(f"{indent}{var}.MultisampleEnable = {me};")
    ctx.emit(f"{indent}{var}.AntialiasedLineEnable = {aae};")
    fsc = sd_uint(obj, "ForcedSampleCount", 0)
    ctx.emit(f"{indent}{var}.ForcedSampleCount = {fsc}u;")
    crm = sd_enum_str(obj, "ConservativeRaster", "D3D12_CONSERVATIVE_RASTERIZATION_MODE_OFF")
    ctx.emit(f"{indent}{var}.ConservativeRaster = {enum_token(crm)};")


def _emit_depth_stencil(ctx, obj, var: str, indent: str = "    ") -> None:
    if obj is None:
        return
    de = 1 if sd_value(sd_child(obj, "DepthEnable")) else 0
    se = 1 if sd_value(sd_child(obj, "StencilEnable")) else 0
    ctx.emit(f"{indent}{var}.DepthEnable = {de};")
    _emit_assigns(ctx, indent, var, obj, [
        ("DepthWriteMask", "enum", "D3D12_DEPTH_WRITE_MASK_ALL"),
        ("DepthFunc", "enum", "D3D12_COMPARISON_FUNC_LESS"),
    ])
    ctx.emit(f"{indent}{var}.StencilEnable = {se};")
    smr = sd_uint(obj, "StencilReadMask", 0xFF)
    smw = sd_uint(obj, "StencilWriteMask", 0xFF)
    ctx.emit(f"{indent}{var}.StencilReadMask = (UINT8){smr};")
    ctx.emit(f"{indent}{var}.StencilWriteMask = (UINT8){smw};")
    for face in ("FrontFace", "BackFace"):
        f = sd_child(obj, face)
        if f is None:
            continue
        sf = sd_enum_str(f, "StencilFailOp", "D3D12_STENCIL_OP_KEEP")
        sd = sd_enum_str(f, "StencilDepthFailOp", "D3D12_STENCIL_OP_KEEP")
        sp = sd_enum_str(f, "StencilPassOp", "D3D12_STENCIL_OP_KEEP")
        sfn = sd_enum_str(f, "StencilFunc", "D3D12_COMPARISON_FUNC_ALWAYS")
        ctx.emit(f"{indent}{var}.{face}.StencilFailOp = {enum_token(sf)};")
        ctx.emit(f"{indent}{var}.{face}.StencilDepthFailOp = {enum_token(sd)};")
        ctx.emit(f"{indent}{var}.{face}.StencilPassOp = {enum_token(sp)};")
        ctx.emit(f"{indent}{var}.{face}.StencilFunc = {enum_token(sfn)};")


def _emit_input_layout(ctx, obj, prefix: str, indent: str = "    ") -> None:
    """Emit a static array for the input layout, then point ``prefix.InputLayout``
    at it. Uses a local static so the lifetime survives the desc-build scope.
    """
    if obj is None:
        return
    elems = sd_child(obj, "pInputElementDescs")
    if elems is None or elems.NumChildren() == 0:
        ctx.emit(f"{indent}{prefix}.InputLayout.pInputElementDescs = nullptr;")
        ctx.emit(f"{indent}{prefix}.InputLayout.NumElements = 0;")
        return
    n = elems.NumChildren()
    # Build a brace-initialised array of D3D12_INPUT_ELEMENT_DESC.
    # SemanticName needs a stable pointer; we emit a wide-scope static.
    ctx.emit(f"{indent}static const D3D12_INPUT_ELEMENT_DESC kElems_{id(obj) & 0xFFFFFF:x}[{n}] = {{")
    arr_name = f"kElems_{id(obj) & 0xFFFFFF:x}"
    for i in range(n):
        e = elems.GetChild(i)
        sem = sd_value(sd_child(e, "SemanticName")) or ""
        sidx = sd_uint(e, "SemanticIndex", 0)
        fmt = sd_enum_str(e, "Format", "DXGI_FORMAT_UNKNOWN")
        islot = sd_uint(e, "InputSlot", 0)
        bo = sd_uint(e, "AlignedByteOffset", 0)
        klass = sd_enum_str(e, "InputSlotClass", "D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA")
        stp = sd_uint(e, "InstanceDataStepRate", 0)
        ctx.emit(f"{indent}  {{ {cpp_string(str(sem))}, {sidx}, {enum_token(fmt)}, "
                 f"{islot}, {bo}, {enum_token(klass)}, {stp} }},")
    ctx.emit(f"{indent}}};")
    ctx.emit(f"{indent}{prefix}.InputLayout.pInputElementDescs = {arr_name};")
    ctx.emit(f"{indent}{prefix}.InputLayout.NumElements = {n};")


def _emit_shader_byte_code(
    ctx, shader_obj, var: str, label: str,
    pso_rid_str: Optional[str] = None, stage: Optional[str] = None,
    indent: str = "    ",
) -> None:
    """Populate a D3D12_SHADER_BYTECODE field with the corresponding blob.

    Looks up the (pso_rid, stage) -> hash mapping built by
    ``_extract_pso_chunk_shader_hashes`` and emits::

        auto <stage>blob = LoadBlob("shaders/<hash>.cso");
        var.pShaderBytecode = <stage>blob.data();
        var.BytecodeLength = <stage>blob.size();

    The local blob is scoped to the surrounding ``{ ... }`` block — typically
    the PSO desc emitter — so it lives until ``CreateXxxPipelineState`` is
    called.

    If the bytecode wasn't extracted (write_blobs=False, or the lookup
    failed), falls back to a TODO comment that points at the right file.
    """
    if shader_obj is None:
        return
    bc_len = sd_uint(shader_obj, "BytecodeLength", 0)
    if bc_len == 0:
        return
    blob_hash = None
    if pso_rid_str is not None and stage is not None:
        blob_hash = ctx.pso_shader_hashes.get((pso_rid_str, stage))
    if blob_hash is None:
        # Fall back to extracting the bytes inline (per-chunk path may have
        # been skipped if write_blobs=False or the chunk shape was unusual).
        if ctx.write_blobs:
            bc = sd_child(shader_obj, "pShaderBytecode")
            raw = sd_byte_array(bc) if bc is not None else None
            if raw:
                blob_hash = _write_shader_blob(ctx, raw)
    if blob_hash is None:
        ctx.emit(f"{indent}// TODO {label}: load {bc_len}-byte shader blob from shaders/<hash>.cso "
                 f"and set {var}.pShaderBytecode / .BytecodeLength.")
        return
    # Derive a local variable name from the field path so multiple stages of
    # the same PSO don't collide. e.g. "d.VS" -> "vsBlob", "d.CS" -> "csBlob".
    short = var.rsplit(".", 1)[-1].lower() + "Blob"
    ctx.emit(f"{indent}auto {short} = LoadBlob(\"shaders/{blob_hash}.cso\");")
    ctx.emit(f"{indent}{var}.pShaderBytecode = {short}.data();")
    ctx.emit(f"{indent}{var}.BytecodeLength = {short}.size();")


def _emit_pso_common_tail(ctx, desc_obj, var: str, indent: str = "    ") -> None:
    """Emit fields shared by graphics + compute PSO desc tails: NodeMask,
    CachedPSO, Flags.
    """
    ctx.emit(f"{indent}{var}.NodeMask = 0u;")
    ctx.emit(f"{indent}{var}.CachedPSO.pCachedBlob = nullptr; {var}.CachedPSO.CachedBlobSizeInBytes = 0;")
    flags = sd_enum_str(desc_obj, "Flags", "D3D12_PIPELINE_STATE_FLAG_NONE")
    ctx.emit(f"{indent}{var}.Flags = {enum_token(flags)};")


@emitter("ID3D12Device::CreateGraphicsPipelineState")
def emit_create_graphics_pso(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pPipelineState")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "PipelineState", "ID3D12PipelineState")
    desc = sd_child(chunk, "pDesc")
    if desc is None:
        ctx.emit(f"  /* CreateGraphicsPipelineState: missing pDesc */")
        return
    rid_str = str(rid)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_GRAPHICS_PIPELINE_STATE_DESC d = {{}};")
    rs = sd_resource(desc, "pRootSignature")
    if rs is not None:
        ctx.emit(f"    d.pRootSignature = {use_resource(ctx, rs, register_as=('RootSignature', 'ID3D12RootSignature'))};")
    for stage_field, label in (("VS", "VS bytecode"), ("PS", "PS bytecode"),
                                ("DS", "DS bytecode"), ("HS", "HS bytecode"),
                                ("GS", "GS bytecode")):
        _emit_shader_byte_code(ctx, sd_child(desc, stage_field), f"d.{stage_field}", label,
                                pso_rid_str=rid_str, stage=stage_field)
    _emit_blend_state(ctx, sd_child(desc, "BlendState"), "d.BlendState")
    sm = sd_uint(desc, "SampleMask", 0xFFFFFFFF)
    ctx.emit(f"    d.SampleMask = {sm}u;")
    _emit_rasterizer(ctx, sd_child(desc, "RasterizerState"), "d.RasterizerState")
    _emit_depth_stencil(ctx, sd_child(desc, "DepthStencilState"), "d.DepthStencilState")
    _emit_input_layout(ctx, sd_child(desc, "InputLayout"), "d")
    ibsc = sd_enum_str(desc, "IBStripCutValue", "D3D12_INDEX_BUFFER_STRIP_CUT_VALUE_DISABLED")
    ctx.emit(f"    d.IBStripCutValue = {enum_token(ibsc)};")
    topo = sd_enum_str(desc, "PrimitiveTopologyType", "D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE")
    ctx.emit(f"    d.PrimitiveTopologyType = {enum_token(topo)};")
    num_rts = sd_uint(desc, "NumRenderTargets", 0)
    ctx.emit(f"    d.NumRenderTargets = {num_rts}u;")
    rts = sd_child(desc, "RTVFormats")
    if rts is not None:
        for i in range(min(rts.NumChildren(), 8)):
            f = sd_value(rts.GetChild(i))
            ctx.emit(f"    d.RTVFormats[{i}] = {enum_token(str(f) if f else 'DXGI_FORMAT_UNKNOWN')};")
    dsv_fmt = sd_enum_str(desc, "DSVFormat", "DXGI_FORMAT_UNKNOWN")
    ctx.emit(f"    d.DSVFormat = {enum_token(dsv_fmt)};")
    sd_obj = sd_child(desc, "SampleDesc")
    if sd_obj is not None:
        ctx.emit(f"    d.SampleDesc.Count = {sd_uint(sd_obj, 'Count', 1)}u;")
        ctx.emit(f"    d.SampleDesc.Quality = {sd_uint(sd_obj, 'Quality', 0)}u;")
    _emit_pso_common_tail(ctx, desc, "d")
    ctx.emit(f"    HR(device->CreateGraphicsPipelineState(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateComputePipelineState")
def emit_create_compute_pso(ctx: ExportContext, chunk) -> None:
    rid = sd_resource(chunk, "pPipelineState")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "PipelineState", "ID3D12PipelineState")
    desc = sd_child(chunk, "pDesc")
    if desc is None:
        ctx.emit(f"  /* CreateComputePipelineState: missing pDesc */")
        return
    rid_str = str(rid)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_COMPUTE_PIPELINE_STATE_DESC d = {{}};")
    rs = sd_resource(desc, "pRootSignature")
    if rs is not None:
        ctx.emit(f"    d.pRootSignature = {use_resource(ctx, rs, register_as=('RootSignature', 'ID3D12RootSignature'))};")
    _emit_shader_byte_code(ctx, sd_child(desc, "CS"), "d.CS", "CS bytecode",
                           pso_rid_str=rid_str, stage="CS")
    _emit_pso_common_tail(ctx, desc, "d")
    ctx.emit(f"    HR(device->CreateComputePipelineState(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


@emitter("ID3D12Device2::CreatePipelineState")
def emit_create_pipeline_state_stream(ctx: ExportContext, chunk) -> None:
    """Convert an expanded stream-PSO chunk back to the simpler graphics or
    compute pipeline state desc and call CreateXxxPipelineState. Mesh shader
    PSOs (AS/MS) still need the real stream API, so those get a TODO stub —
    the reconstructed graphics desc is preserved in a comment for the user.
    """
    rid = sd_resource(chunk, "pPipelineState")
    if rid is None:
        return
    name = declare_resource(ctx, rid, "PipelineState", "ID3D12PipelineState")
    desc = sd_child(chunk, "pDesc")
    if desc is None:
        ctx.emit(f"  /* CreatePipelineState (stream): missing pDesc for {name} */")
        return

    cs_obj = sd_child(desc, "CS")
    cs_len = sd_uint(cs_obj, "BytecodeLength", 0) if cs_obj is not None else 0
    as_obj = sd_child(desc, "AS")
    as_len = sd_uint(as_obj, "BytecodeLength", 0) if as_obj is not None else 0
    ms_obj = sd_child(desc, "MS")
    ms_len = sd_uint(ms_obj, "BytecodeLength", 0) if ms_obj is not None else 0

    rid_str = str(rid)
    if cs_len > 0:
        # Compute pipeline path
        ctx.emit(f"  {{")
        ctx.emit(f"    D3D12_COMPUTE_PIPELINE_STATE_DESC d = {{}};")
        rs = sd_resource(desc, "pRootSignature")
        if rs is not None:
            ctx.emit(f"    d.pRootSignature = {use_resource(ctx, rs, register_as=('RootSignature', 'ID3D12RootSignature'))};")
        _emit_shader_byte_code(ctx, cs_obj, "d.CS", "CS bytecode (stream PSO)",
                               pso_rid_str=rid_str, stage="CS")
        _emit_pso_common_tail(ctx, desc, "d")
        ctx.emit(f"    HR(device->CreateComputePipelineState(&d, IID_PPV_ARGS(&{name})));")
        ctx.emit(f"  }}")
        return

    if as_len > 0 or ms_len > 0:
        # Mesh shader PSO — needs the real stream API. Stub out for now but
        # surface the subobject shape so the user can wire it up.
        ctx.emit(
            f"  /* TODO mesh-shader PSO {name}: reconstruct D3D12_PIPELINE_STATE_STREAM_DESC "
            f"with AS={as_len}B + MS={ms_len}B + PS={sd_uint(sd_child(desc, 'PS'), 'BytecodeLength', 0)}B */"
        )
        return

    # Graphics pipeline path — emit via CreateGraphicsPipelineState.
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_GRAPHICS_PIPELINE_STATE_DESC d = {{}};")
    rs = sd_resource(desc, "pRootSignature")
    if rs is not None:
        ctx.emit(f"    d.pRootSignature = {use_resource(ctx, rs, register_as=('RootSignature', 'ID3D12RootSignature'))};")
    for stage_field, label in (("VS", "VS bytecode"), ("PS", "PS bytecode"),
                                ("DS", "DS bytecode"), ("HS", "HS bytecode"),
                                ("GS", "GS bytecode")):
        _emit_shader_byte_code(ctx, sd_child(desc, stage_field), f"d.{stage_field}",
                               label + " (stream PSO)",
                               pso_rid_str=rid_str, stage=stage_field)
    _emit_blend_state(ctx, sd_child(desc, "BlendState"), "d.BlendState")
    sm = sd_uint(desc, "SampleMask", 0xFFFFFFFF)
    ctx.emit(f"    d.SampleMask = {sm}u;")
    _emit_rasterizer(ctx, sd_child(desc, "RasterizerState"), "d.RasterizerState")
    _emit_depth_stencil(ctx, sd_child(desc, "DepthStencilState"), "d.DepthStencilState")
    _emit_input_layout(ctx, sd_child(desc, "InputLayout"), "d")
    ibsc = sd_enum_str(desc, "IBStripCutValue", "D3D12_INDEX_BUFFER_STRIP_CUT_VALUE_DISABLED")
    ctx.emit(f"    d.IBStripCutValue = {enum_token(ibsc)};")
    topo = sd_enum_str(desc, "PrimitiveTopologyType", "D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE")
    ctx.emit(f"    d.PrimitiveTopologyType = {enum_token(topo)};")
    num_rts = sd_uint(desc, "NumRenderTargets", 0)
    ctx.emit(f"    d.NumRenderTargets = {num_rts}u;")
    rts = sd_child(desc, "RTVFormats")
    if rts is not None:
        for i in range(min(rts.NumChildren(), 8)):
            f = sd_value(rts.GetChild(i))
            ctx.emit(f"    d.RTVFormats[{i}] = {enum_token(str(f) if f else 'DXGI_FORMAT_UNKNOWN')};")
    dsv_fmt = sd_enum_str(desc, "DSVFormat", "DXGI_FORMAT_UNKNOWN")
    ctx.emit(f"    d.DSVFormat = {enum_token(dsv_fmt)};")
    sd_obj = sd_child(desc, "SampleDesc")
    if sd_obj is not None:
        ctx.emit(f"    d.SampleDesc.Count = {sd_uint(sd_obj, 'Count', 1)}u;")
        ctx.emit(f"    d.SampleDesc.Quality = {sd_uint(sd_obj, 'Quality', 0)}u;")
    _emit_pso_common_tail(ctx, desc, "d")
    ctx.emit(f"    HR(device->CreateGraphicsPipelineState(&d, IID_PPV_ARGS(&{name})));")
    ctx.emit(f"  }}")


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


def _emit_resource_desc(ctx, desc, var: str, indent: str = "    ") -> None:
    """Populate a D3D12_RESOURCE_DESC at ``var`` from a serialised
    ``D3D12_RESOURCE_DESC`` (or RESOURCE_DESC1) SDObject.
    """
    dim = sd_enum_str(desc, "Dimension", "D3D12_RESOURCE_DIMENSION_BUFFER")
    fmt = sd_enum_str(desc, "Format", "DXGI_FORMAT_UNKNOWN")
    layout = sd_enum_str(desc, "Layout", "D3D12_TEXTURE_LAYOUT_UNKNOWN")
    flags = sd_enum_str(desc, "Flags", "D3D12_RESOURCE_FLAG_NONE")
    samples = sd_uint(sd_child(desc, "SampleDesc"), "Count", 1)
    sample_q = sd_uint(sd_child(desc, "SampleDesc"), "Quality", 0)
    ctx.emit(f"{indent}{var}.Dimension = {enum_token(dim)};")
    ctx.emit(f"{indent}{var}.Alignment = {sd_uint(desc, 'Alignment', 0)}ull;")
    ctx.emit(f"{indent}{var}.Width = {sd_uint(desc, 'Width', 0)}ull;")
    ctx.emit(f"{indent}{var}.Height = {sd_uint(desc, 'Height', 1)}u;")
    ctx.emit(f"{indent}{var}.DepthOrArraySize = (UINT16){sd_uint(desc, 'DepthOrArraySize', 1)};")
    ctx.emit(f"{indent}{var}.MipLevels = (UINT16){sd_uint(desc, 'MipLevels', 1)};")
    ctx.emit(f"{indent}{var}.Format = {enum_token(fmt)};")
    ctx.emit(f"{indent}{var}.SampleDesc.Count = {samples}u;")
    ctx.emit(f"{indent}{var}.SampleDesc.Quality = {sample_q}u;")
    ctx.emit(f"{indent}{var}.Layout = {enum_token(layout)};")
    ctx.emit(f"{indent}{var}.Flags = {enum_token(flags)};")


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
    ctx.emit(f"    D3D12_RESOURCE_DESC d = {{}};")
    _emit_resource_desc(ctx, desc, "d")
    ctx.emit(
        f"    HR(device->CreatePlacedResource({use_resource(ctx, heap)}, {offset}ull, &d, "
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


# ---------------------------------------------------------------------------
# View desc populators
#
# Each populator emits C++ that fills a local variable of the appropriate
# D3D12_*_VIEW_DESC type. They write into a buffer of lines that the caller
# splices into its emit() output. The ``var`` argument is the C++ name of the
# local variable being populated.
#
# RenderDoc serialises every view desc with the same field names as the D3D12
# SDK struct members, plus a per-dimension subobject named after the
# ViewDimension enumerator (Buffer/Texture1D/Texture2D/Texture2DArray/...).
# We follow that mapping exactly so the generated code compiles against the
# stock D3D12 headers.
# ---------------------------------------------------------------------------

_DIM_TO_SUBOBJ = {
    # SRV
    "D3D12_SRV_DIMENSION_BUFFER": "Buffer",
    "D3D12_SRV_DIMENSION_TEXTURE1D": "Texture1D",
    "D3D12_SRV_DIMENSION_TEXTURE1DARRAY": "Texture1DArray",
    "D3D12_SRV_DIMENSION_TEXTURE2D": "Texture2D",
    "D3D12_SRV_DIMENSION_TEXTURE2DARRAY": "Texture2DArray",
    "D3D12_SRV_DIMENSION_TEXTURE2DMS": "Texture2DMS",
    "D3D12_SRV_DIMENSION_TEXTURE2DMSARRAY": "Texture2DMSArray",
    "D3D12_SRV_DIMENSION_TEXTURE3D": "Texture3D",
    "D3D12_SRV_DIMENSION_TEXTURECUBE": "TextureCube",
    "D3D12_SRV_DIMENSION_TEXTURECUBEARRAY": "TextureCubeArray",
    "D3D12_SRV_DIMENSION_RAYTRACING_ACCELERATION_STRUCTURE": "RaytracingAccelerationStructure",
    # UAV
    "D3D12_UAV_DIMENSION_BUFFER": "Buffer",
    "D3D12_UAV_DIMENSION_TEXTURE1D": "Texture1D",
    "D3D12_UAV_DIMENSION_TEXTURE1DARRAY": "Texture1DArray",
    "D3D12_UAV_DIMENSION_TEXTURE2D": "Texture2D",
    "D3D12_UAV_DIMENSION_TEXTURE2DARRAY": "Texture2DArray",
    "D3D12_UAV_DIMENSION_TEXTURE2DMS": "Texture2DMS",
    "D3D12_UAV_DIMENSION_TEXTURE2DMSARRAY": "Texture2DMSArray",
    "D3D12_UAV_DIMENSION_TEXTURE3D": "Texture3D",
    # RTV
    "D3D12_RTV_DIMENSION_BUFFER": "Buffer",
    "D3D12_RTV_DIMENSION_TEXTURE1D": "Texture1D",
    "D3D12_RTV_DIMENSION_TEXTURE1DARRAY": "Texture1DArray",
    "D3D12_RTV_DIMENSION_TEXTURE2D": "Texture2D",
    "D3D12_RTV_DIMENSION_TEXTURE2DARRAY": "Texture2DArray",
    "D3D12_RTV_DIMENSION_TEXTURE2DMS": "Texture2DMS",
    "D3D12_RTV_DIMENSION_TEXTURE2DMSARRAY": "Texture2DMSArray",
    "D3D12_RTV_DIMENSION_TEXTURE3D": "Texture3D",
    # DSV
    "D3D12_DSV_DIMENSION_TEXTURE1D": "Texture1D",
    "D3D12_DSV_DIMENSION_TEXTURE1DARRAY": "Texture1DArray",
    "D3D12_DSV_DIMENSION_TEXTURE2D": "Texture2D",
    "D3D12_DSV_DIMENSION_TEXTURE2DARRAY": "Texture2DArray",
    "D3D12_DSV_DIMENSION_TEXTURE2DMS": "Texture2DMS",
    "D3D12_DSV_DIMENSION_TEXTURE2DMSARRAY": "Texture2DMSArray",
}


def _emit_assigns(ctx: "ExportContext", indent: str, lhs_prefix: str, obj, fields):
    """Emit ``{lhs_prefix}.<field> = <value>;`` for each (name, kind, default)
    in ``fields``. ``kind`` is ``"uint"`` / ``"int"`` / ``"float"`` / ``"enum"``.
    """
    if obj is None:
        return
    for entry in fields:
        if len(entry) == 3:
            name, kind, default = entry
        else:
            name, kind = entry
            default = 0 if kind in ("uint", "int") else (0.0 if kind == "float" else "")
        if kind == "uint":
            v = sd_uint(obj, name, default)
            ctx.emit(f"{indent}{lhs_prefix}.{name} = {v}u;")
        elif kind == "int":
            v = sd_int(obj, name, default)
            ctx.emit(f"{indent}{lhs_prefix}.{name} = {v};")
        elif kind == "uint64":
            v = sd_uint(obj, name, default)
            ctx.emit(f"{indent}{lhs_prefix}.{name} = {v}ull;")
        elif kind == "float":
            v = sd_float(obj, name, default)
            ctx.emit(f"{indent}{lhs_prefix}.{name} = {cpp_float(v)};")
        elif kind == "enum":
            v = sd_enum_str(obj, name, default)
            ctx.emit(f"{indent}{lhs_prefix}.{name} = {enum_token(v)};")


def emit_srv_desc(ctx: "ExportContext", desc_obj, var: str, indent: str = "    ") -> None:
    """Populate a D3D12_SHADER_RESOURCE_VIEW_DESC into local var ``var``."""
    fmt = sd_enum_str(desc_obj, "Format", "DXGI_FORMAT_UNKNOWN")
    dim = sd_enum_str(desc_obj, "ViewDimension", "D3D12_SRV_DIMENSION_UNKNOWN")
    mapping = sd_uint(desc_obj, "Shader4ComponentMapping", 0x1688)
    ctx.emit(f"{indent}{var}.Format = {enum_token(fmt)};")
    ctx.emit(f"{indent}{var}.ViewDimension = {enum_token(dim)};")
    ctx.emit(f"{indent}{var}.Shader4ComponentMapping = {mapping}u;")
    sub_name = _DIM_TO_SUBOBJ.get(dim)
    if sub_name is None:
        return
    sub = sd_child(desc_obj, sub_name)
    if sub is None:
        return
    lhs = f"{var}.{sub_name}"
    if sub_name == "Buffer":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstElement", "uint64"), ("NumElements", "uint"),
            ("StructureByteStride", "uint"), ("Flags", "enum", "D3D12_BUFFER_SRV_FLAG_NONE"),
        ])
    elif sub_name in ("Texture1D", "TextureCube"):
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MostDetailedMip", "uint"), ("MipLevels", "uint"),
            ("ResourceMinLODClamp", "float"),
        ])
    elif sub_name == "Texture1DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MostDetailedMip", "uint"), ("MipLevels", "uint"),
            ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
            ("ResourceMinLODClamp", "float"),
        ])
    elif sub_name == "Texture2D":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MostDetailedMip", "uint"), ("MipLevels", "uint"),
            ("PlaneSlice", "uint"), ("ResourceMinLODClamp", "float"),
        ])
    elif sub_name == "Texture2DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MostDetailedMip", "uint"), ("MipLevels", "uint"),
            ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
            ("PlaneSlice", "uint"), ("ResourceMinLODClamp", "float"),
        ])
    elif sub_name == "Texture3D":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MostDetailedMip", "uint"), ("MipLevels", "uint"),
            ("ResourceMinLODClamp", "float"),
        ])
    elif sub_name == "TextureCubeArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MostDetailedMip", "uint"), ("MipLevels", "uint"),
            ("First2DArrayFace", "uint"), ("NumCubes", "uint"),
            ("ResourceMinLODClamp", "float"),
        ])
    elif sub_name == "Texture2DMS":
        ctx.emit(f"{indent}// Texture2DMS variant has no fields")
    elif sub_name == "Texture2DMSArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "RaytracingAccelerationStructure":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("Location", "uint64"),
        ])


def emit_uav_desc(ctx: "ExportContext", desc_obj, var: str, indent: str = "    ") -> None:
    fmt = sd_enum_str(desc_obj, "Format", "DXGI_FORMAT_UNKNOWN")
    dim = sd_enum_str(desc_obj, "ViewDimension", "D3D12_UAV_DIMENSION_UNKNOWN")
    ctx.emit(f"{indent}{var}.Format = {enum_token(fmt)};")
    ctx.emit(f"{indent}{var}.ViewDimension = {enum_token(dim)};")
    sub_name = _DIM_TO_SUBOBJ.get(dim)
    if sub_name is None:
        return
    sub = sd_child(desc_obj, sub_name)
    if sub is None:
        return
    lhs = f"{var}.{sub_name}"
    if sub_name == "Buffer":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstElement", "uint64"), ("NumElements", "uint"),
            ("StructureByteStride", "uint"), ("CounterOffsetInBytes", "uint64"),
            ("Flags", "enum", "D3D12_BUFFER_UAV_FLAG_NONE"),
        ])
    elif sub_name == "Texture1D":
        _emit_assigns(ctx, indent, lhs, sub, [("MipSlice", "uint")])
    elif sub_name == "Texture1DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "Texture2D":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("PlaneSlice", "uint"),
        ])
    elif sub_name == "Texture2DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstArraySlice", "uint"),
            ("ArraySize", "uint"), ("PlaneSlice", "uint"),
        ])
    elif sub_name == "Texture2DMS":
        ctx.emit(f"{indent}// Texture2DMS variant has no fields")
    elif sub_name == "Texture2DMSArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "Texture3D":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstWSlice", "uint"), ("WSize", "uint"),
        ])


def emit_rtv_desc(ctx: "ExportContext", desc_obj, var: str, indent: str = "    ") -> None:
    fmt = sd_enum_str(desc_obj, "Format", "DXGI_FORMAT_UNKNOWN")
    dim = sd_enum_str(desc_obj, "ViewDimension", "D3D12_RTV_DIMENSION_UNKNOWN")
    ctx.emit(f"{indent}{var}.Format = {enum_token(fmt)};")
    ctx.emit(f"{indent}{var}.ViewDimension = {enum_token(dim)};")
    sub_name = _DIM_TO_SUBOBJ.get(dim)
    if sub_name is None:
        return
    sub = sd_child(desc_obj, sub_name)
    if sub is None:
        return
    lhs = f"{var}.{sub_name}"
    if sub_name == "Buffer":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstElement", "uint64"), ("NumElements", "uint"),
        ])
    elif sub_name == "Texture1D":
        _emit_assigns(ctx, indent, lhs, sub, [("MipSlice", "uint")])
    elif sub_name == "Texture1DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "Texture2D":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("PlaneSlice", "uint"),
        ])
    elif sub_name == "Texture2DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstArraySlice", "uint"),
            ("ArraySize", "uint"), ("PlaneSlice", "uint"),
        ])
    elif sub_name == "Texture2DMS":
        ctx.emit(f"{indent}// Texture2DMS variant has no fields")
    elif sub_name == "Texture2DMSArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "Texture3D":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstWSlice", "uint"), ("WSize", "uint"),
        ])


def emit_dsv_desc(ctx: "ExportContext", desc_obj, var: str, indent: str = "    ") -> None:
    fmt = sd_enum_str(desc_obj, "Format", "DXGI_FORMAT_UNKNOWN")
    flags = sd_enum_str(desc_obj, "Flags", "D3D12_DSV_FLAG_NONE")
    dim = sd_enum_str(desc_obj, "ViewDimension", "D3D12_DSV_DIMENSION_UNKNOWN")
    ctx.emit(f"{indent}{var}.Format = {enum_token(fmt)};")
    ctx.emit(f"{indent}{var}.Flags = {enum_token(flags)};")
    ctx.emit(f"{indent}{var}.ViewDimension = {enum_token(dim)};")
    sub_name = _DIM_TO_SUBOBJ.get(dim)
    if sub_name is None:
        return
    sub = sd_child(desc_obj, sub_name)
    if sub is None:
        return
    lhs = f"{var}.{sub_name}"
    if sub_name == "Texture1D":
        _emit_assigns(ctx, indent, lhs, sub, [("MipSlice", "uint")])
    elif sub_name == "Texture1DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "Texture2D":
        _emit_assigns(ctx, indent, lhs, sub, [("MipSlice", "uint")])
    elif sub_name == "Texture2DArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("MipSlice", "uint"), ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])
    elif sub_name == "Texture2DMS":
        ctx.emit(f"{indent}// Texture2DMS variant has no fields")
    elif sub_name == "Texture2DMSArray":
        _emit_assigns(ctx, indent, lhs, sub, [
            ("FirstArraySlice", "uint"), ("ArraySize", "uint"),
        ])


def emit_sampler_desc(ctx: "ExportContext", desc_obj, var: str, indent: str = "    ") -> None:
    """Populate a D3D12_SAMPLER_DESC from the chunk's sampler desc subobject.

    RenderDoc internally uses D3D12_SAMPLER_DESC2 but the field layout is
    compatible with D3D12_SAMPLER_DESC up through MaxLOD; we ignore the
    Sampler2-only Flags field and FloatBorderColor union (using the float
    variant which matches the captured data).
    """
    filt = sd_enum_str(desc_obj, "Filter", "D3D12_FILTER_MIN_MAG_MIP_LINEAR")
    addr_u = sd_enum_str(desc_obj, "AddressU", "D3D12_TEXTURE_ADDRESS_MODE_CLAMP")
    addr_v = sd_enum_str(desc_obj, "AddressV", "D3D12_TEXTURE_ADDRESS_MODE_CLAMP")
    addr_w = sd_enum_str(desc_obj, "AddressW", "D3D12_TEXTURE_ADDRESS_MODE_CLAMP")
    bias = cpp_float(sd_float(desc_obj, "MipLODBias", 0.0))
    aniso = sd_uint(desc_obj, "MaxAnisotropy", 1)
    cmp = sd_enum_str(desc_obj, "ComparisonFunc", "D3D12_COMPARISON_FUNC_NEVER")
    min_lod = cpp_float(sd_float(desc_obj, "MinLOD", 0.0))
    max_lod = cpp_float(sd_float(desc_obj, "MaxLOD", 3.402823466e+38))
    border = sd_child(desc_obj, "FloatBorderColor") or sd_child(desc_obj, "BorderColor")
    if border is not None and border.NumChildren() >= 4:
        bvs = [cpp_float(sd_value(border.GetChild(i)) or 0.0) for i in range(4)]
    else:
        bvs = ["0.0f", "0.0f", "0.0f", "0.0f"]
    ctx.emit(f"{indent}{var}.Filter = {enum_token(filt)};")
    ctx.emit(f"{indent}{var}.AddressU = {enum_token(addr_u)};")
    ctx.emit(f"{indent}{var}.AddressV = {enum_token(addr_v)};")
    ctx.emit(f"{indent}{var}.AddressW = {enum_token(addr_w)};")
    ctx.emit(f"{indent}{var}.MipLODBias = {bias};")
    ctx.emit(f"{indent}{var}.MaxAnisotropy = {aniso}u;")
    ctx.emit(f"{indent}{var}.ComparisonFunc = {enum_token(cmp)};")
    ctx.emit(f"{indent}{var}.BorderColor[0] = {bvs[0]};")
    ctx.emit(f"{indent}{var}.BorderColor[1] = {bvs[1]};")
    ctx.emit(f"{indent}{var}.BorderColor[2] = {bvs[2]};")
    ctx.emit(f"{indent}{var}.BorderColor[3] = {bvs[3]};")
    ctx.emit(f"{indent}{var}.MinLOD = {min_lod};")
    ctx.emit(f"{indent}{var}.MaxLOD = {max_lod};")


def _view_desc_subobject(chunk):
    """Locate the ``desc`` SDObject (the D3D12Descriptor struct serialised by
    Serialise_DynamicDescriptorWrite). Falls back to ``pDesc`` if the chunk
    doesn't follow the DynamicDescriptorWrite layout.
    """
    return sd_child(chunk, "desc") or sd_child(chunk, "pDesc")


def _view_destination(chunk):
    """Locate the destination PortableHandle on a view-creation chunk."""
    return sd_child(chunk, "dst") or sd_child(chunk, "DestDescriptor")


def _view_resource(chunk):
    """Locate the view's target resource ID from the chunk's `desc.Resource`
    field (Serialise_DynamicDescriptorWrite layout) or the explicit `pResource`
    argument when the chunk has one.
    """
    desc = _view_desc_subobject(chunk)
    if desc is not None:
        rid = sd_resource(desc, "Resource")
        if rid is not None:
            return rid
    return sd_resource(chunk, "pResource")


@emitter("ID3D12Device::CreateConstantBufferView")
def emit_create_cbv(ctx, chunk):
    dst = cpu_handle_expr(ctx, _view_destination(chunk))
    desc = _view_desc_subobject(chunk)
    cbv = sd_child(desc, "Descriptor") if desc is not None else None
    addr = sd_uint(cbv, "BufferLocation", 0)
    size = sd_uint(cbv, "SizeInBytes", 0)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_CONSTANT_BUFFER_VIEW_DESC vd = {{ {addr}ull, {size} }};")
    ctx.emit(f"    device->CreateConstantBufferView(&vd, {dst});")
    ctx.emit(f"  }}")


def _view_inner_desc(chunk):
    """The view-specific desc (D3D12_*_VIEW_DESC) lives at chunk.desc.Descriptor
    for Create*View chunks. Returns the SDObject or None if missing.
    """
    outer = _view_desc_subobject(chunk)
    if outer is None:
        return None
    return sd_child(outer, "Descriptor")


@emitter("ID3D12Device::CreateShaderResourceView")
def emit_create_srv(ctx, chunk):
    dst = cpu_handle_expr(ctx, _view_destination(chunk))
    res = _view_resource(chunk)
    res_arg = use_resource(ctx, res, register_as=("Texture2D", "ID3D12Resource"))
    inner = _view_inner_desc(chunk)
    if inner is None:
        ctx.emit(f"  device->CreateShaderResourceView({res_arg}, nullptr, {dst});")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_SHADER_RESOURCE_VIEW_DESC vd = {{}};")
    emit_srv_desc(ctx, inner, "vd")
    ctx.emit(f"    device->CreateShaderResourceView({res_arg}, &vd, {dst});")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateUnorderedAccessView")
def emit_create_uav(ctx, chunk):
    dst = cpu_handle_expr(ctx, _view_destination(chunk))
    res = _view_resource(chunk)
    desc = _view_desc_subobject(chunk)
    counter = sd_resource(desc, "CounterResource") if desc is not None else None
    res_arg = use_resource(ctx, res, register_as=("Texture2D", "ID3D12Resource"))
    cnt_arg = use_resource(ctx, counter, register_as=("Buffer", "ID3D12Resource")) if counter is not None else "nullptr"
    inner = _view_inner_desc(chunk)
    if inner is None:
        ctx.emit(f"  device->CreateUnorderedAccessView({res_arg}, {cnt_arg}, nullptr, {dst});")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_UNORDERED_ACCESS_VIEW_DESC vd = {{}};")
    emit_uav_desc(ctx, inner, "vd")
    ctx.emit(f"    device->CreateUnorderedAccessView({res_arg}, {cnt_arg}, &vd, {dst});")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateRenderTargetView")
def emit_create_rtv(ctx, chunk):
    dst = cpu_handle_expr(ctx, _view_destination(chunk))
    res = _view_resource(chunk)
    res_arg = use_resource(ctx, res, register_as=("Texture2D", "ID3D12Resource"))
    inner = _view_inner_desc(chunk)
    if inner is None:
        ctx.emit(f"  device->CreateRenderTargetView({res_arg}, nullptr, {dst});")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_RENDER_TARGET_VIEW_DESC vd = {{}};")
    emit_rtv_desc(ctx, inner, "vd")
    ctx.emit(f"    device->CreateRenderTargetView({res_arg}, &vd, {dst});")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateDepthStencilView")
def emit_create_dsv(ctx, chunk):
    dst = cpu_handle_expr(ctx, _view_destination(chunk))
    res = _view_resource(chunk)
    res_arg = use_resource(ctx, res, register_as=("Texture2D", "ID3D12Resource"))
    inner = _view_inner_desc(chunk)
    if inner is None:
        ctx.emit(f"  device->CreateDepthStencilView({res_arg}, nullptr, {dst});")
        return
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_DEPTH_STENCIL_VIEW_DESC vd = {{}};")
    emit_dsv_desc(ctx, inner, "vd")
    ctx.emit(f"    device->CreateDepthStencilView({res_arg}, &vd, {dst});")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CreateSampler")
@emitter("ID3D12Device11::CreateSampler2")
def emit_create_sampler(ctx, chunk):
    dst = cpu_handle_expr(ctx, _view_destination(chunk))
    inner = _view_inner_desc(chunk)
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_SAMPLER_DESC sd = {{}};")
    if inner is not None:
        emit_sampler_desc(ctx, inner, "sd")
    else:
        ctx.emit(f"    sd.Filter = D3D12_FILTER_MIN_MAG_MIP_LINEAR;")
        ctx.emit(f"    sd.AddressU = sd.AddressV = sd.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;")
        ctx.emit(f"    sd.MaxAnisotropy = 1;")
        ctx.emit(f"    sd.ComparisonFunc = D3D12_COMPARISON_FUNC_NEVER;")
        ctx.emit(f"    sd.MinLOD = 0.0f; sd.MaxLOD = D3D12_FLOAT32_MAX;")
    ctx.emit(f"    device->CreateSampler(&sd, {dst});")
    ctx.emit(f"  }}")


@emitter("ID3D12Device::CopyDescriptors")
@emitter("ID3D12Device::CopyDescriptorsSimple")
def emit_copy_descriptors(ctx: ExportContext, chunk) -> None:
    # Both chunk kinds serialise as a `DescriptorCopies` array of
    # { type, dst (PortableHandle), src (PortableHandle) } via
    # Serialise_DynamicDescriptorCopies. We expand each entry to a one-slot
    # CopyDescriptorsSimple at replay; the captured driver does the same.
    copies = sd_child(chunk, "DescriptorCopies")
    if copies is None or copies.NumChildren() == 0:
        ctx.emit(f"  /* CopyDescriptors: empty DescriptorCopies array */")
        return
    n = copies.NumChildren()
    for i in range(n):
        c = copies.GetChild(i)
        if c is None:
            continue
        # The type field on each entry is a D3D12_DESCRIPTOR_HEAP_TYPE.
        heap_type = sd_enum_str(c, "type", "D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV")
        dst = cpu_handle_expr(ctx, sd_child(c, "dst"))
        src = cpu_handle_expr(ctx, sd_child(c, "src"))
        ctx.emit(
            f"  device->CopyDescriptorsSimple(1, {dst}, {src}, {enum_token(heap_type)});"
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
    cv = cmdlist_var(ctx, chunk)
    barriers = sd_child(chunk, "pBarriers")
    if barriers is None or barriers.NumChildren() == 0:
        ctx.emit(f"  {cv}->ResourceBarrier(0, nullptr);")
        return
    n = barriers.NumChildren()
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_RESOURCE_BARRIER bs[{n}] = {{}};")
    for i in range(n):
        b = barriers.GetChild(i)
        typ = sd_enum_str(b, "Type", "D3D12_RESOURCE_BARRIER_TYPE_TRANSITION")
        flags = sd_enum_str(b, "Flags", "D3D12_RESOURCE_BARRIER_FLAG_NONE")
        ctx.emit(f"    bs[{i}].Type = {enum_token(typ)};")
        ctx.emit(f"    bs[{i}].Flags = {enum_token(flags)};")
        if "TRANSITION" in typ:
            t = sd_child(b, "Transition")
            res = use_resource(ctx, sd_resource(t, "pResource"),
                               register_as=("Texture2D", "ID3D12Resource"))
            sub = sd_uint(t, "Subresource", 0xFFFFFFFF)
            sb = sd_enum_str(t, "StateBefore", "D3D12_RESOURCE_STATE_COMMON")
            sa = sd_enum_str(t, "StateAfter", "D3D12_RESOURCE_STATE_COMMON")
            ctx.emit(f"    bs[{i}].Transition.pResource = {res};")
            ctx.emit(f"    bs[{i}].Transition.Subresource = {sub}u;")
            ctx.emit(f"    bs[{i}].Transition.StateBefore = {enum_token(sb)};")
            ctx.emit(f"    bs[{i}].Transition.StateAfter = {enum_token(sa)};")
        elif "ALIASING" in typ:
            a = sd_child(b, "Aliasing")
            rb = use_resource(ctx, sd_resource(a, "pResourceBefore"),
                              register_as=("Texture2D", "ID3D12Resource"))
            ra = use_resource(ctx, sd_resource(a, "pResourceAfter"),
                              register_as=("Texture2D", "ID3D12Resource"))
            ctx.emit(f"    bs[{i}].Aliasing.pResourceBefore = {rb};")
            ctx.emit(f"    bs[{i}].Aliasing.pResourceAfter = {ra};")
        elif "UAV" in typ:
            u = sd_child(b, "UAV")
            res = use_resource(ctx, sd_resource(u, "pResource"),
                               register_as=("Texture2D", "ID3D12Resource"))
            ctx.emit(f"    bs[{i}].UAV.pResource = {res};")
    ctx.emit(f"    {cv}->ResourceBarrier({n}, bs);")
    ctx.emit(f"  }}")


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
    handle = gpu_handle_expr(ctx, sd_child(chunk, "BaseDescriptor"))
    ctx.emit(f"  {cv}->{method}({idx}, {handle});")


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
    cv = cmdlist_var(ctx, chunk)
    num = sd_uint(chunk, "NumRenderTargetDescriptors", 0)
    rts_contiguous = bool(sd_value(sd_child(chunk, "RTsSingleHandleToDescriptorRange")))
    rts = sd_child(chunk, "pRenderTargetDescriptors")
    dsv = sd_child(chunk, "pDepthStencilDescriptor")
    if rts is None or num == 0:
        rt_block = "0, nullptr"
    else:
        # Build an array literal of CPU handles. If RTsSingleHandleToDescriptorRange
        # is true the original code passed a single handle; we still expand
        # each slot explicitly so the runtime call is unambiguous.
        ctx.emit(f"  {{")
        ctx.emit(f"    D3D12_CPU_DESCRIPTOR_HANDLE rts[{num if num > 0 else 1}] = {{}};")
        for i in range(min(num, rts.NumChildren())):
            ctx.emit(f"    rts[{i}] = {cpu_handle_expr(ctx, rts.GetChild(i))};")
        if dsv is not None:
            dsv_expr = cpu_handle_expr(ctx, dsv)
            ctx.emit(
                f"    D3D12_CPU_DESCRIPTOR_HANDLE dsv = {dsv_expr};"
            )
            dsv_ref = "&dsv"
        else:
            dsv_ref = "nullptr"
        contig = "TRUE" if rts_contiguous else "FALSE"
        ctx.emit(f"    {cv}->OMSetRenderTargets({num}, rts, {contig}, {dsv_ref});")
        ctx.emit(f"  }}")
        return
    # Empty path
    if dsv is not None:
        dsv_expr = cpu_handle_expr(ctx, dsv)
        ctx.emit(
            f"  {{ D3D12_CPU_DESCRIPTOR_HANDLE dsv = {dsv_expr};"
            f" {cv}->OMSetRenderTargets(0, nullptr, FALSE, &dsv); }}"
        )
    else:
        ctx.emit(f"  {cv}->OMSetRenderTargets(0, nullptr, FALSE, nullptr);")


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
        ctx.emit(f"  /* ClearRenderTargetView: missing color */")
        return
    vals = [cpp_float(sd_value(color.GetChild(i)) or 0.0) for i in range(4)]
    handle = cpu_handle_expr(ctx, sd_child(chunk, "RenderTargetView"))
    ctx.emit(
        f"  {{ FLOAT c[4] = {{ {', '.join(vals)} }};"
        f" {cmdlist_var(ctx, chunk)}->ClearRenderTargetView({handle}, c, 0, nullptr); }}"
    )


@emitter("ID3D12GraphicsCommandList::ClearDepthStencilView")
def emit_clear_dsv(ctx, chunk):
    flags = sd_enum_str(chunk, "ClearFlags", "D3D12_CLEAR_FLAG_DEPTH")
    depth = cpp_float(sd_float(chunk, "Depth", 1.0))
    stencil = sd_uint(chunk, "Stencil", 0)
    handle = cpu_handle_expr(ctx, sd_child(chunk, "DepthStencilView"))
    ctx.emit(
        f"  {cmdlist_var(ctx, chunk)}->ClearDepthStencilView({handle}, "
        f"{enum_token(flags)}, {depth}, {stencil}, 0, nullptr);"
    )


def _emit_clear_uav(ctx, chunk, *, float_variant: bool) -> None:
    cv = cmdlist_var(ctx, chunk)
    gpu = gpu_handle_expr(ctx, sd_child(chunk, "ViewGPUHandleInCurrentHeap"))
    cpu = cpu_handle_expr(ctx, sd_child(chunk, "ViewCPUHandle"))
    res = use_resource(ctx, sd_resource(chunk, "pResource"))
    values = sd_child(chunk, "Values")
    if values is None or values.NumChildren() < 4:
        ctx.emit(f"  /* ClearUnorderedAccessView{'Float' if float_variant else 'Uint'}: missing Values[4] */")
        return
    if float_variant:
        vals = [cpp_float(sd_value(values.GetChild(i)) or 0.0) for i in range(4)]
        ctx.emit(
            f"  {{ FLOAT v[4] = {{ {', '.join(vals)} }};"
            f" {cv}->ClearUnorderedAccessViewFloat({gpu}, {cpu}, {res}, v, 0, nullptr); }}"
        )
    else:
        vals = [str(int(sd_value(values.GetChild(i)) or 0) & 0xFFFFFFFF) + "u" for i in range(4)]
        ctx.emit(
            f"  {{ UINT v[4] = {{ {', '.join(vals)} }};"
            f" {cv}->ClearUnorderedAccessViewUint({gpu}, {cpu}, {res}, v, 0, nullptr); }}"
        )


@emitter("ID3D12GraphicsCommandList::ClearUnorderedAccessViewUint")
def emit_clear_uav_uint(ctx, chunk):
    _emit_clear_uav(ctx, chunk, float_variant=False)


@emitter("ID3D12GraphicsCommandList::ClearUnorderedAccessViewFloat")
def emit_clear_uav_float(ctx, chunk):
    _emit_clear_uav(ctx, chunk, float_variant=True)


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


def _emit_texture_copy_location(ctx, obj, var: str, indent: str = "    ") -> None:
    """Emit C++ that populates a local ``D3D12_TEXTURE_COPY_LOCATION`` from a
    serialised ``D3D12_TEXTURE_COPY_LOCATION`` SDObject."""
    res = use_resource(ctx, sd_resource(obj, "pResource"),
                       register_as=("Texture2D", "ID3D12Resource"))
    typ = sd_enum_str(obj, "Type", "D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX")
    ctx.emit(f"{indent}{var}.pResource = {res};")
    ctx.emit(f"{indent}{var}.Type = {enum_token(typ)};")
    if "PLACED_FOOTPRINT" in typ:
        pf = sd_child(obj, "PlacedFootprint")
        offset = sd_uint(pf, "Offset", 0)
        fp = sd_child(pf, "Footprint")
        fmt = sd_enum_str(fp, "Format", "DXGI_FORMAT_UNKNOWN")
        ctx.emit(f"{indent}{var}.PlacedFootprint.Offset = {offset}ull;")
        ctx.emit(f"{indent}{var}.PlacedFootprint.Footprint.Format = {enum_token(fmt)};")
        ctx.emit(f"{indent}{var}.PlacedFootprint.Footprint.Width = {sd_uint(fp, 'Width', 0)}u;")
        ctx.emit(f"{indent}{var}.PlacedFootprint.Footprint.Height = {sd_uint(fp, 'Height', 0)}u;")
        ctx.emit(f"{indent}{var}.PlacedFootprint.Footprint.Depth = {sd_uint(fp, 'Depth', 1)}u;")
        ctx.emit(f"{indent}{var}.PlacedFootprint.Footprint.RowPitch = {sd_uint(fp, 'RowPitch', 0)}u;")
    else:
        ctx.emit(f"{indent}{var}.SubresourceIndex = {sd_uint(obj, 'SubresourceIndex', 0)}u;")


@emitter("ID3D12GraphicsCommandList::CopyTextureRegion")
def emit_copy_texture_region(ctx, chunk):
    cv = cmdlist_var(ctx, chunk)
    dst = sd_child(chunk, "dst")
    src = sd_child(chunk, "src")
    dst_x = sd_uint(chunk, "DstX", 0)
    dst_y = sd_uint(chunk, "DstY", 0)
    dst_z = sd_uint(chunk, "DstZ", 0)
    box = sd_child(chunk, "pSrcBox")
    ctx.emit(f"  {{")
    ctx.emit(f"    D3D12_TEXTURE_COPY_LOCATION dst = {{}};")
    _emit_texture_copy_location(ctx, dst, "dst")
    ctx.emit(f"    D3D12_TEXTURE_COPY_LOCATION src = {{}};")
    _emit_texture_copy_location(ctx, src, "src")
    if box is not None:
        ctx.emit(f"    D3D12_BOX box = {{ {sd_uint(box, 'left')}u, {sd_uint(box, 'top')}u, "
                 f"{sd_uint(box, 'front')}u, {sd_uint(box, 'right')}u, "
                 f"{sd_uint(box, 'bottom')}u, {sd_uint(box, 'back')}u }};")
        ctx.emit(f"    {cv}->CopyTextureRegion(&dst, {dst_x}, {dst_y}, {dst_z}, &src, &box);")
    else:
        ctx.emit(f"    {cv}->CopyTextureRegion(&dst, {dst_x}, {dst_y}, {dst_z}, &src, nullptr);")
    ctx.emit(f"  }}")


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

// ---------------------------------------------------------------------------
// Descriptor handle helpers
//
// RenderDoc serialises every D3D12 CPU/GPU descriptor handle as a
// "PortableHandle" pair of (heap ResourceId, slot index). At replay we
// rematerialise the actual handle by combining the heap's runtime base
// address with the descriptor stride for that heap's type:
//
//     heap->GetCPUDescriptorHandleForHeapStart() +
//         slot * device->GetDescriptorHandleIncrementSize(heap->GetDesc().Type)
//
// These helpers do that math once, without forcing the generated code to
// repeat the boilerplate at every binding.
// ---------------------------------------------------------------------------

static inline D3D12_CPU_DESCRIPTOR_HANDLE CpuHandle(ID3D12DescriptorHeap *heap, UINT slot) {
    if(!heap) return D3D12_CPU_DESCRIPTOR_HANDLE{0};
    D3D12_CPU_DESCRIPTOR_HANDLE h = heap->GetCPUDescriptorHandleForHeapStart();
    h.ptr += SIZE_T(slot) * device->GetDescriptorHandleIncrementSize(heap->GetDesc().Type);
    return h;
}

static inline D3D12_GPU_DESCRIPTOR_HANDLE GpuHandle(ID3D12DescriptorHeap *heap, UINT slot) {
    if(!heap) return D3D12_GPU_DESCRIPTOR_HANDLE{0};
    D3D12_GPU_DESCRIPTOR_HANDLE h = heap->GetGPUDescriptorHandleForHeapStart();
    h.ptr += UINT64(slot) * device->GetDescriptorHandleIncrementSize(heap->GetDesc().Type);
    return h;
}

// Reads a blob file (root signature, shader bytecode, etc.) from disk into a
// std::vector. Returns an empty vector on failure.
#include <vector>
static inline std::vector<uint8_t> LoadBlob(const char *path) {
    std::vector<uint8_t> out;
    FILE *f = std::fopen(path, "rb");
    if(!f) {
        std::fprintf(stderr, "LoadBlob: failed to open %s\n", path);
        return out;
    }
    std::fseek(f, 0, SEEK_END);
    long n = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    if(n > 0) {
        out.resize((size_t)n);
        std::fread(out.data(), 1, (size_t)n, f);
    }
    std::fclose(f);
    return out;
}

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
        f.write("  // The global `device` ComPtr is initialised by main.cpp before this\n")
        f.write("  // call; the local alias keeps emitter output uniform.\n")
        f.write("  ID3D12Device *device_raw = dev; (void)device_raw;\n")
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


_STAGE_ENUM_TO_FIELD = {
    "Vertex": "VS",
    "Hull": "HS",
    "Domain": "DS",
    "Geometry": "GS",
    "Pixel": "PS",
    "Compute": "CS",
    "Amplification": "AS",
    "Mesh": "MS",
}


def _write_shader_blob(ctx: ExportContext, raw: bytes) -> str:
    """Hash ``raw``, write it to ``shaders/<md5>.cso`` (deduping), and return
    the hash. Skips the disk write when ``ctx.write_blobs`` is off but still
    returns the hash so the PSO emitter can reference it.
    """
    h = _lib.shader_bytecode_hash(raw)
    if h in ctx.shader_blobs:
        return h
    ctx.shader_blobs[h] = raw
    if ctx.write_blobs:
        path = os.path.join(ctx.out_dir, "shaders", f"{h}.cso")
        with open(path, "wb") as f:
            f.write(raw)
    return h


def _extract_shader_blobs(controller, ctx: ExportContext) -> None:
    """Walk every action in the capture, pull each bound shader's reflection,
    write the raw bytecode to ``shaders/<md5>.cso``, and record both:

      - ``ctx.shader_id_to_hash[shader_resource_id] = hash``
      - ``ctx.pso_shader_hashes[(pso_resource_id, stage_field)] = hash``

    The PSO emitter uses the second mapping to emit ``LoadBlob(...)`` calls
    that reconstitute D3D12_SHADER_BYTECODE at replay.

    Reflection rather than the structured file is used because RenderDoc
    serialises ``D3D12_SHADER_BYTECODE.pShaderBytecode`` as an opaque buffer
    that the Python-side SDFile view doesn't expose. The reflection path
    does expose ``rawBytes``, and the (PSO_id, stage) -> shader_id binding
    is recoverable from the live pipeline state at any draw/dispatch event.
    """
    if not ctx.write_blobs:
        return
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
        # Identify the PSO bound at this event. D3D12 exposes the wrapped
        # pipeline state resource via D3D12Pipe::State.pipelineResourceId.
        pso_rid_str: Optional[str] = None
        try:
            d3d12 = controller.GetD3D12PipelineState()
        except Exception:
            d3d12 = None
        if d3d12 is not None:
            try:
                pso_rid = d3d12.pipelineResourceId
                if pso_rid is not None and str(pso_rid) not in ("ResourceId()", "0"):
                    pso_rid_str = str(pso_rid)
            except Exception:
                pass

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
            if refl is None or len(refl.rawBytes) == 0:
                continue
            raw = bytes(refl.rawBytes)
            h = _write_shader_blob(ctx, raw)
            ctx.shader_id_to_hash[str(refl.resourceId)] = h
            if pso_rid_str is not None:
                stage_field = _STAGE_ENUM_TO_FIELD.get(_lib.shader_stage_name(stage_enum))
                if stage_field is not None:
                    ctx.pso_shader_hashes[(pso_rid_str, stage_field)] = h


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
