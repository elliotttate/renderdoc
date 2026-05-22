"""PSO swap harness generator — emit ready-to-paste C++ for D3D12 PSO swap.

For UEVR / mod patch trials: given a target PSO ResourceId in a capture,
emit:
  1. JSON spec  — root sig pointer expectations, shader-stage byte counts,
                  bound RT formats, viewport, descriptor table heap+offset
                  expectations
  2. C++ snippet — drop-in code that:
       - reconstructs the source PSO's D3D12_GRAPHICS_PIPELINE_STATE_DESC
       - clones it with a substituted shader bytecode (provided file path)
       - hooks SetPipelineState() and swaps the PSO when the source is bound
       - optionally gates by viewport.x (right-eye only) or event-id range

Output:
    <out>/pso_<id>_spec.json
    <out>/pso_<id>_swap.cpp
    <out>/README.md

Usage from Python:
    from util.automation import pso_swap_harness
    pso_swap_harness.generate(
        capture_path="path.rdc",
        pso_id="ResourceId::12783",
        new_shader_path="patched.dxbc",  # PS by default
        out_dir="harness_out",
        right_eye_only=True,
    )

CLI:
    python util/automation/pso_swap_harness.py <capture.rdc> --pso ResourceId::12783 --new-ps patched.cso --out harness/
"""

import argparse
import json
import os
import sys
from string import Template

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


CPP_TEMPLATE = Template(r"""// Auto-generated PSO swap harness
// Source capture:   $capture_path
// Target PSO:       $pso_id  (PS hash $ps_hash_short, CRC32 $ps_crc32_hex)
// New shader bytes: $new_shader_path
// Generated at:     $timestamp
//
// Hook usage:
//   #include "pso_${pso_id_safe}_swap.cpp"
//   At ID3D12Device::CreateGraphicsPipelineState detour entry, call:
//     PsoSwap_${pso_id_safe}::OnCreatePSO(orig_device, orig_desc, ppPSO, return_value);
//   Then at ID3D12GraphicsCommandList::SetPipelineState detour entry, call:
//     PsoSwap_${pso_id_safe}::OnSetPipelineState(cmdlist, pPSO);
//

#include <d3d12.h>
#include <wrl/client.h>
#include <vector>
#include <cstdint>
#include <fstream>

using Microsoft::WRL::ComPtr;

namespace PsoSwap_${pso_id_safe} {

// === RECOVERED METADATA ===========================================

// Source PSO hash fingerprints (from RenderDoc capture analysis):
//   PS DXBC MD5 (first 16): $ps_hash_short
//   PS DXBC CRC32-zlib:     $ps_crc32_hex
//   VS DXBC MD5 (first 16): $vs_hash_short
//   VS DXBC CRC32-zlib:     $vs_crc32_hex
//   RootSig ResourceId:     $root_sig_id
//
// Bound state at sample event $sample_event_id:
//   Viewport:  ($vp_x, $vp_y, $vp_w x $vp_h)
//   RT0 format: $rt0_format

// Gating: enable for the EYE you want to patch.
//   right_eye_only = true   -> only swap when viewport.x >= half-width
//   right_eye_only = false  -> swap unconditionally
static constexpr bool   k_RightEyeOnly       = $right_eye_only;
static constexpr float  k_RightEyeMinX       = ${right_eye_min_x}f;
static constexpr uint32_t k_PsCrc32          = ${ps_crc32_decimal}u;

// === Patched-shader bytecode load ====================================

static std::vector<uint8_t> g_PatchedPSBytecode;
static bool g_Initialized = false;

static void EnsureInit() {
    if (g_Initialized) return;
    g_Initialized = true;
    std::ifstream f(L"$new_shader_path_escaped", std::ios::binary);
    if (!f) return;
    f.seekg(0, std::ios::end);
    auto sz = (size_t)f.tellg();
    f.seekg(0, std::ios::beg);
    g_PatchedPSBytecode.resize(sz);
    f.read((char*)g_PatchedPSBytecode.data(), sz);
}

// === Source-PSO detection ===========================================

// We identify the source PSO by hashing its PS bytecode at create time
// and matching against k_PsCrc32. This is more robust than matching
// ID3D12PipelineState* pointers across re-creations.

static uint32_t Crc32Zlib(const uint8_t* data, size_t len) {
    // Standard zlib/PNG CRC32 (polynomial 0xEDB88320)
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (int j = 0; j < 8; ++j) {
            crc = (crc >> 1) ^ (0xEDB88320u & -(int32_t)(crc & 1));
        }
    }
    return ~crc;
}

static bool IsTargetPSO(const D3D12_GRAPHICS_PIPELINE_STATE_DESC& desc) {
    if (!desc.PS.pShaderBytecode || desc.PS.BytecodeLength == 0) return false;
    uint32_t crc = Crc32Zlib((const uint8_t*)desc.PS.pShaderBytecode, desc.PS.BytecodeLength);
    return crc == k_PsCrc32;
}

// === The clone-with-patched-PS-shader path ==========================

#include <unordered_map>
static std::unordered_map<ID3D12PipelineState*, ComPtr<ID3D12PipelineState>> g_PSOMap;

void OnCreatePSO(ID3D12Device* device,
                 const D3D12_GRAPHICS_PIPELINE_STATE_DESC* desc,
                 ID3D12PipelineState** ppPSO,
                 HRESULT createResult) {
    EnsureInit();
    if (createResult != S_OK || !desc || !ppPSO || !*ppPSO) return;
    if (!IsTargetPSO(*desc)) return;
    if (g_PatchedPSBytecode.empty()) return;
    // Clone the desc with patched PS bytecode
    D3D12_GRAPHICS_PIPELINE_STATE_DESC patched = *desc;
    patched.PS.pShaderBytecode = g_PatchedPSBytecode.data();
    patched.PS.BytecodeLength  = g_PatchedPSBytecode.size();
    ComPtr<ID3D12PipelineState> clone;
    HRESULT hr = device->CreateGraphicsPipelineState(&patched, IID_PPV_ARGS(&clone));
    if (FAILED(hr)) return;
    g_PSOMap[*ppPSO] = clone;
}

// === Swap at SetPipelineState =======================================

static D3D12_VIEWPORT g_LastViewport = {};

void OnRSSetViewports(UINT n, const D3D12_VIEWPORT* vps) {
    if (n > 0 && vps) g_LastViewport = vps[0];
}

bool ShouldSwap() {
    if (!k_RightEyeOnly) return true;
    return g_LastViewport.TopLeftX >= k_RightEyeMinX;
}

ID3D12PipelineState* OnSetPipelineState(ID3D12PipelineState* pPSO) {
    auto it = g_PSOMap.find(pPSO);
    if (it == g_PSOMap.end()) return pPSO;
    if (!ShouldSwap()) return pPSO;
    return it->second.Get();
}

} // namespace PsoSwap_${pso_id_safe}
""")


def _safe_pso_id(pso_id: str) -> str:
    return pso_id.replace("::", "_").replace(":", "_").replace(" ", "_")


def _hexify_path(path: str) -> str:
    return path.replace("\\", "\\\\")


def collect_metadata(capture_path: str, pso_id: str) -> dict:
    """Walk the capture to find an event using the target PSO + collect its state."""
    cap, controller = _lib.open_capture(capture_path)
    try:
        import hashlib, zlib, time
        sample_event = None
        for a in _lib.walk_actions(controller):
            flags = int(a.flags)
            if not (flags & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            eid = int(a.eventId)
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
            except Exception:
                continue
            try:
                if _lib.resource_id_str(d3d12.pipelineResourceId) == pso_id:
                    sample_event = eid
                    break
            except Exception:
                continue
        if sample_event is None:
            return {"error": f"No event found using PSO {pso_id}"}
        controller.SetFrameEvent(sample_event, True)
        d3d12 = controller.GetD3D12PipelineState()
        pipe = controller.GetPipelineState()
        meta = {
            "capture_path": os.path.abspath(capture_path),
            "pso_id": pso_id,
            "sample_event_id": sample_event,
            "root_sig_id": _lib.resource_id_str(d3d12.rootSignature.resourceId),
        }
        # Shader hashes
        for stage_enum, key in (
            (rd.ShaderStage.Pixel,  "ps"),
            (rd.ShaderStage.Vertex, "vs"),
            (rd.ShaderStage.Compute, "cs"),
        ):
            try:
                refl = pipe.GetShaderReflection(stage_enum)
            except Exception:
                refl = None
            if refl and len(refl.rawBytes) > 0:
                raw = bytes(refl.rawBytes)
                meta[f"{key}_entry"] = str(refl.entryPoint)
                md5 = hashlib.md5(raw).hexdigest()
                meta[f"{key}_md5"] = md5
                meta[f"{key}_md5_short"] = md5[:16]
                cz = zlib.crc32(raw) & 0xFFFFFFFF
                meta[f"{key}_crc32_zlib"] = cz
                meta[f"{key}_crc32_zlib_hex"] = f"0x{cz:08X}"
                meta[f"{key}_size"] = len(raw)
        # Viewport / RT format
        try:
            if len(d3d12.rasterizer.viewports) > 0:
                v = d3d12.rasterizer.viewports[0]
                meta["viewport"] = {"x": float(v.x), "y": float(v.y),
                                      "w": float(v.width), "h": float(v.height)}
        except Exception: pass
        try:
            rt = d3d12.outputMerger.renderTargets[0]
            rid = _lib.resource_id_str(rt.resource)
            for t in controller.GetTextures():
                if _lib.resource_id_str(t.resourceId) == rid:
                    meta["rt0"] = {"resource_id": rid,
                                     "width": int(t.width), "height": int(t.height),
                                     "format": str(t.format.Name())}
                    break
        except Exception: pass
        # Descriptor tables
        try:
            tables = []
            for i, p in enumerate(d3d12.rootSignature.parameters):
                try:
                    heap = _lib.resource_id_str(p.heap)
                    if heap and heap != "ResourceId::0":
                        tables.append({
                            "root_index": i,
                            "visibility": str(p.visibility).split(".")[-1],
                            "heap": heap,
                            "heap_byte_offset": int(p.heapByteOffset),
                        })
                except Exception: pass
            meta["descriptor_tables"] = tables
        except Exception: pass
        meta["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return meta
    finally:
        controller.Shutdown()
        cap.Shutdown()


def generate(capture_path: str, pso_id: str, new_shader_path: str,
             out_dir: str = None, right_eye_only: bool = True,
             right_eye_min_x: float = 600.0) -> dict:
    out_dir = out_dir or f"pso_swap_{_safe_pso_id(pso_id)}"
    os.makedirs(out_dir, exist_ok=True)
    meta = collect_metadata(capture_path, pso_id)
    if meta.get("error"):
        raise RuntimeError(meta["error"])
    pso_id_safe = _safe_pso_id(pso_id)
    cpp = CPP_TEMPLATE.substitute(
        capture_path=os.path.abspath(capture_path),
        pso_id=pso_id,
        pso_id_safe=pso_id_safe,
        timestamp=meta["timestamp"],
        new_shader_path=os.path.abspath(new_shader_path),
        new_shader_path_escaped=_hexify_path(os.path.abspath(new_shader_path)),
        ps_hash_short=meta.get("ps_md5_short", "?"),
        ps_crc32_hex=meta.get("ps_crc32_zlib_hex", "0x0"),
        ps_crc32_decimal=meta.get("ps_crc32_zlib", 0),
        vs_hash_short=meta.get("vs_md5_short", "?"),
        vs_crc32_hex=meta.get("vs_crc32_zlib_hex", "0x0"),
        root_sig_id=meta.get("root_sig_id", "?"),
        sample_event_id=meta.get("sample_event_id", 0),
        vp_x=meta.get("viewport", {}).get("x", 0),
        vp_y=meta.get("viewport", {}).get("y", 0),
        vp_w=meta.get("viewport", {}).get("w", 0),
        vp_h=meta.get("viewport", {}).get("h", 0),
        rt0_format=meta.get("rt0", {}).get("format", "?"),
        right_eye_only="true" if right_eye_only else "false",
        right_eye_min_x=right_eye_min_x,
    )
    cpp_path = os.path.join(out_dir, f"pso_{pso_id_safe}_swap.cpp")
    json_path = os.path.join(out_dir, f"pso_{pso_id_safe}_spec.json")
    readme_path = os.path.join(out_dir, "README.md")
    with open(cpp_path, "w", encoding="utf-8") as f:
        f.write(cpp)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(_readme(pso_id, meta, right_eye_only, right_eye_min_x))
    return {"cpp": cpp_path, "json": json_path, "readme": readme_path, "meta": meta}


def _readme(pso_id, meta, right_eye_only, right_eye_min_x):
    return f"""# PSO Swap Harness — `{pso_id}`

Generated from `{meta['capture_path']}`
Sample event: **{meta['sample_event_id']}**
Root signature: `{meta.get('root_sig_id')}`

## Target shader fingerprints

| Stage | Entry | MD5 (first 16) | CRC32-zlib |
|---|---|---|---|
| PS | {meta.get('ps_entry')} | `{meta.get('ps_md5_short')}` | `{meta.get('ps_crc32_zlib_hex')}` |
| VS | {meta.get('vs_entry')} | `{meta.get('vs_md5_short')}` | `{meta.get('vs_crc32_zlib_hex')}` |

The harness identifies the target PSO at `CreateGraphicsPipelineState` time
by computing the CRC32-zlib of the bound PS bytecode and comparing against
the recovered `k_PsCrc32`.

## Gating

| | Value |
|---|---|
| Right-eye-only swap | `{str(right_eye_only).lower()}` |
| Right-eye min viewport.x | `{right_eye_min_x}` |

If `right_eye_only` is true, the swapped PSO is only used when
`viewport.x >= {right_eye_min_x}` at the `SetPipelineState` site.

## How to wire it

1. Compile your patched PS to `.cso` (DXBC) and place it at the path that
   was passed as `--new-ps`.
2. `#include` the generated `.cpp` from your hook layer.
3. At your `ID3D12Device::CreateGraphicsPipelineState` detour, call:
   ```cpp
   PsoSwap_{_safe_pso_id(pso_id)}::OnCreatePSO(device, desc, ppPSO, hresult);
   ```
4. At `ID3D12GraphicsCommandList::RSSetViewports`, mirror:
   ```cpp
   PsoSwap_{_safe_pso_id(pso_id)}::OnRSSetViewports(n, viewports);
   ```
5. At `ID3D12GraphicsCommandList::SetPipelineState`, replace the bound PSO:
   ```cpp
   pPSO = PsoSwap_{_safe_pso_id(pso_id)}::OnSetPipelineState(pPSO);
   ```

## Bound state at sample event

- Viewport: `({meta.get('viewport', {}).get('x')}, {meta.get('viewport', {}).get('y')}, {meta.get('viewport', {}).get('w')} x {meta.get('viewport', {}).get('h')})`
- RT0 format: `{meta.get('rt0', {}).get('format')}`
- Descriptor tables: {len(meta.get('descriptor_tables', []))}

See `{_safe_pso_id(pso_id)}_spec.json` for the full root-sig / descriptor-table layout.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--pso", required=True, help="target ResourceId (e.g. ResourceId::12783)")
    ap.add_argument("--new-ps", required=True, help="path to patched PS .cso/.dxbc")
    ap.add_argument("--out", help="output dir")
    ap.add_argument("--all-eyes", action="store_true", help="swap unconditionally (not right-eye-only)")
    ap.add_argument("--right-eye-min-x", type=float, default=600.0)
    args = ap.parse_args()
    r = generate(args.capture, args.pso, args.new_ps, args.out,
                  right_eye_only=not args.all_eyes,
                  right_eye_min_x=args.right_eye_min_x)
    print(f"Generated:")
    print(f"  C++:    {r['cpp']}")
    print(f"  JSON:   {r['json']}")
    print(f"  README: {r['readme']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
