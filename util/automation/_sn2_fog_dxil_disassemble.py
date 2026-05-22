"""Disassemble the 4 UWE fog CS shaders and identify register usage.

For each shader:
  1. Use RenderDoc's controller.DisassembleShader to get DXIL/DXBC text
  2. Save text to disk alongside the .dxbc binary
  3. Parse the disassembly to identify:
     - Which u# is each UAV output
     - Which t# is each SRV input
     - Which b# is the View constant buffer
     - Atomic / structured / RWTexture2D ops on each UAV
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_phase1_spec"
SHADER_DIR = os.path.join(OUT_DIR, "fog_shaders")
LOG = os.path.join(OUT_DIR, "fog_dxil.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

TARGET_EVENTS = [
    (21348, "UWEFogReconstructCS"),
    (21370, "UWEFogDenoiseCS_read1"),
    (21377, "UWEFogDenoiseCS_write"),
    (21390, "UWEFogResolveCS"),
]


def parse_register_usage(text):
    """Extract bound resources and shader I/O from RDEF / structured DXIL header."""
    info = {
        "cbvs": [],     # (name, register, space)
        "srvs": [],     # (name, register, space, type)
        "uavs": [],     # (name, register, space, type)
        "samplers": [],
        "writes": [],   # (uav_register, op_name) — actual write ops the shader performs
        "reads": [],    # (resource_register, op_name) — sample/load ops
    }
    # Match the RDEF resource binding section (DXBC format)
    # Standard format: // <name> <type>[<spec>] <regbase>:<count>
    for line in text.splitlines():
        line_strip = line.strip()
        # Common DXBC format: "// Name              Type  Format    Dim  HLSL Bind  Count"
        # E.g.:               "// FogVolumeUAV      UAV   float4    2d   u0         1"
        m = re.match(r'^//\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S*)\s+([bsutc])(\d+)\s+(\d+)', line_strip)
        if m:
            name, type_, fmt, dim, prefix, reg, count = m.groups()
            entry = {"name": name, "type": type_, "format": fmt, "dim": dim,
                     "register": int(reg), "count": int(count)}
            if prefix == "b":
                info["cbvs"].append(entry)
            elif prefix == "t":
                info["srvs"].append(entry)
            elif prefix == "u":
                info["uavs"].append(entry)
            elif prefix == "s":
                info["samplers"].append(entry)
        # Write operations:
        # Match DXIL: call ... @dx.op.bufferStore... or @dx.op.textureStore...
        # Also DXBC: store_uav_typed u0.xyzw,
        if "store_uav_typed" in line_strip or "store_structured" in line_strip:
            m2 = re.search(r'\bu(\d+)\b', line_strip)
            if m2:
                op = "store_uav_typed" if "store_uav_typed" in line_strip else "store_structured"
                info["writes"].append({"register": int(m2.group(1)), "op": op})
        # DXIL: textureStore / bufferStore intrinsics
        if "textureStore" in line_strip or "bufferStore" in line_strip:
            m2 = re.search(r'\bU(\d+)\b|\bU\[(\d+)\]', line_strip)
            if m2:
                reg = m2.group(1) or m2.group(2)
                op = "textureStore" if "textureStore" in line_strip else "bufferStore"
                info["writes"].append({"register": int(reg), "op": op})
        # Sample ops:
        if "sample_l" in line_strip or "sample_b" in line_strip or "sample(" in line_strip:
            m2 = re.search(r'\bt(\d+)\b', line_strip)
            if m2:
                info["reads"].append({"register": int(m2.group(1)), "op": "sample"})
        if "ld(" in line_strip or "ld_uav_typed" in line_strip:
            m2 = re.search(r'\b[tu](\d+)\b', line_strip)
            if m2:
                info["reads"].append({"register": int(m2.group(1)), "op": "load"})
    # Deduplicate write ops by register
    seen = set()
    dedup_writes = []
    for w in info["writes"]:
        key = (w["register"], w["op"])
        if key in seen: continue
        seen.add(key)
        dedup_writes.append(w)
    info["writes"] = dedup_writes
    return info


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    # Get available disassembly targets
    try:
        targets = controller.GetDisassemblyTargets(True)
        target_names = [str(t) for t in targets]
        log(f"  available DisassembleShader targets: {target_names}")
    except Exception as e:
        log(f"  could not query targets: {e}")
        target_names = ["DXBC", "DXIL", "AMD"]

    spec = {}
    for eid, label in TARGET_EVENTS:
        log(f"\n=== event {eid} ({label}) ===")
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            if not refl or len(refl.rawBytes) == 0:
                log("  no reflection")
                continue
            h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
            log(f"  hash={h[:16]} entry={refl.entryPoint} size={len(refl.rawBytes)}")

            # Try each disassembly target until one returns text
            best_text = None
            chosen_target = None
            for tgt in target_names:
                try:
                    text = controller.DisassembleShader(rd.ResourceId(), refl, tgt)
                    if text and len(text) > 100:
                        best_text = text
                        chosen_target = tgt
                        break
                except Exception:
                    continue
            if not best_text:
                log("  all DisassembleShader targets failed")
                continue
            log(f"  disassembled with target='{chosen_target}' ({len(best_text)} chars)")

            # Save text — sanitize target name (it may contain "/")
            safe_tgt = chosen_target.replace("/", "_").replace(" ", "_")
            txt_path = os.path.join(SHADER_DIR, f"{label}_{h[:16]}.{safe_tgt}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(best_text)
            log(f"  saved {txt_path}")

            # Parse bound resources from reflection (more reliable than parsing text)
            res_info = {
                "cbvs": [],
                "srvs": [],
                "uavs": [],
                "samplers": [],
                "entry": str(refl.entryPoint),
                "hash": h,
                "size": len(refl.rawBytes),
            }
            try:
                for cb in refl.constantBlocks:
                    res_info["cbvs"].append({
                        "name": str(cb.name),
                        "register": int(cb.fixedBindNumber) if hasattr(cb, "fixedBindNumber") else None,
                        "space": int(cb.fixedBindSetOrSpace) if hasattr(cb, "fixedBindSetOrSpace") else None,
                        "byteSize": int(cb.byteSize),
                        "varCount": len(cb.variables),
                    })
            except Exception:
                pass
            try:
                for s in refl.readOnlyResources:
                    res_info["srvs"].append({
                        "name": str(s.name),
                        "register": int(s.fixedBindNumber) if hasattr(s, "fixedBindNumber") else None,
                        "space": int(s.fixedBindSetOrSpace) if hasattr(s, "fixedBindSetOrSpace") else None,
                        "type": str(s.textureType).split(".")[-1] if hasattr(s, "textureType") else None,
                    })
            except Exception:
                pass
            try:
                for u in refl.readWriteResources:
                    res_info["uavs"].append({
                        "name": str(u.name),
                        "register": int(u.fixedBindNumber) if hasattr(u, "fixedBindNumber") else None,
                        "space": int(u.fixedBindSetOrSpace) if hasattr(u, "fixedBindSetOrSpace") else None,
                        "type": str(u.textureType).split(".")[-1] if hasattr(u, "textureType") else None,
                    })
            except Exception:
                pass
            try:
                for s in refl.samplers:
                    res_info["samplers"].append({
                        "name": str(s.name),
                        "register": int(s.fixedBindNumber) if hasattr(s, "fixedBindNumber") else None,
                    })
            except Exception:
                pass

            log(f"  CBVs ({len(res_info['cbvs'])}):")
            for c in res_info["cbvs"]:
                log(f"    {c.get('name', '?')[:30]:30}  b{c.get('register')} space{c.get('space')}  size={c.get('byteSize')}")
            log(f"  SRVs ({len(res_info['srvs'])}):")
            for s in res_info["srvs"]:
                log(f"    {s.get('name', '?')[:30]:30}  t{s.get('register')} space{s.get('space')}  type={s.get('type')}")
            log(f"  UAVs ({len(res_info['uavs'])}):")
            for u in res_info["uavs"]:
                log(f"    {u.get('name', '?')[:30]:30}  u{u.get('register')} space{u.get('space')}  type={u.get('type')}")
            log(f"  Samplers ({len(res_info['samplers'])}):")
            for s in res_info["samplers"]:
                log(f"    {s.get('name', '?')[:30]:30}  s{s.get('register')}")

            # Also parse the text for write ops
            text_info = parse_register_usage(best_text)
            if text_info["writes"]:
                log(f"  detected write ops in disasm:")
                for w in text_info["writes"][:10]:
                    log(f"    u{w['register']}: {w['op']}")

            spec[label] = {
                "eventId": eid,
                "shader": res_info,
                "textRegisterUsage": text_info,
                "disassemblyTarget": chosen_target,
                "disassemblyPath": txt_path,
            }
        except Exception as e:
            log(f"  ERROR: {e}")
            import traceback
            log(traceback.format_exc())

    out_json = os.path.join(OUT_DIR, "fog_dxil_register_usage.json")
    with open(out_json, "w") as f:
        json.dump(spec, f, indent=2, default=str)
    log(f"\nwrote {out_json}")

    # =================================================================
    # FINAL SUMMARY — UEVR action items
    # =================================================================
    log("\n=== UEVR ACTION SUMMARY ===")
    for label, info in spec.items():
        sh = info.get("shader", {})
        log(f"\n{label}:")
        log(f"  entry={sh.get('entry')}  hash={sh.get('hash', '')[:16]}")
        uav_names = [(u.get('register'), u.get('name'), u.get('type')) for u in sh.get('uavs', [])]
        log(f"  UAVs (writes go to these):")
        for r, n, t in uav_names:
            log(f"    u{r} = '{n}'  type={t}")
        view_cb = [c for c in sh.get('cbvs', []) if c.get('name', '').lower() == 'view']
        if view_cb:
            log(f"  View cbuffer: b{view_cb[0].get('register')}")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
