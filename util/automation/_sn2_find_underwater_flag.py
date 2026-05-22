"""Empirical search for the per-eye control-flow trigger.

Read full View CB on LEFT (eid 14715) and RIGHT (eid 14845) — the actual
visible-bug basepass events. Then surface:
  1. All float positions where LEFT != RIGHT by IPD-scale (1e-3 to 1.0 m)
  2. All positions where one eye is 0 and the other is non-zero (flags)
  3. All positions where the values have OPPOSITE SIGNS (could indicate
     "above water" vs "below water" classification)
  4. Sweep candidate offsets for camera-Y-like values (large magnitude
     floats that differ by small amounts)

Plus retry pixel history at multiple pixel positions on the backbuffer.
"""

import json
import os
import struct
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "underwater_flag.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

# Use the actual visible-bug basepass events
L_EID = 14715
R_EID = 14845


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def read_view_cb(controller, eid):
    controller.SetFrameEvent(eid, True)
    d3d12 = controller.GetD3D12PipelineState()
    for p in d3d12.rootSignature.parameters:
        try:
            d = p.descriptor
        except Exception:
            continue
        if d is None or getattr(d, "resource", None) is None:
            continue
        vis = str(p.visibility).split(".")[-1]
        reg = int(p.reg)
        if vis == "Pixel" and reg == 1:
            return (bytes(controller.GetBufferData(d.resource, int(d.byteOffset), 10240)),
                    res_id(d.resource), int(d.byteOffset))
    return None, None, None


cap, controller = _lib.open_capture(CAPTURE)
try:
    log(f"reading View CB at L={L_EID}, R={R_EID}")
    l_data, l_res, l_off = read_view_cb(controller, L_EID)
    r_data, r_res, r_off = read_view_cb(controller, R_EID)
    log(f"  LEFT  {l_res}+{l_off} ({len(l_data)} bytes)")
    log(f"  RIGHT {r_res}+{r_off} ({len(r_data)} bytes)")
    assert len(l_data) == len(r_data)
    N = len(l_data) // 4

    # =================================================================
    # 1. Find positions where LEFT == 0 BUT RIGHT != 0 (or vice versa)
    # =================================================================
    log("\n=== 1. ZERO vs NONZERO discrepancies (flag candidates) ===")
    zero_vs_nonzero = []
    for i in range(N):
        lf, = struct.unpack("<f", l_data[i * 4:(i + 1) * 4])
        rf, = struct.unpack("<f", r_data[i * 4:(i + 1) * 4])
        li, = struct.unpack("<I", l_data[i * 4:(i + 1) * 4])
        ri, = struct.unpack("<I", r_data[i * 4:(i + 1) * 4])
        if (li == 0) != (ri == 0):
            zero_vs_nonzero.append({"offset": i * 4, "L_f": lf, "R_f": rf,
                                       "L_i": li, "R_i": ri})
    log(f"  found {len(zero_vs_nonzero)} positions:")
    for z in zero_vs_nonzero[:30]:
        log(f"    @+{z['offset']:5d}: L_f={z['L_f']:.6g} R_f={z['R_f']:.6g}  (uint L={z['L_i']} R={z['R_i']})")

    # =================================================================
    # 2. Sign-flipped positions (LEFT positive, RIGHT negative or vice versa)
    # =================================================================
    log("\n=== 2. SIGN-FLIPPED positions (classification candidates) ===")
    sign_flipped = []
    for i in range(N):
        lf, = struct.unpack("<f", l_data[i * 4:(i + 1) * 4])
        rf, = struct.unpack("<f", r_data[i * 4:(i + 1) * 4])
        if (lf > 1e-6 and rf < -1e-6) or (lf < -1e-6 and rf > 1e-6):
            sign_flipped.append({"offset": i * 4, "L": lf, "R": rf})
    log(f"  found {len(sign_flipped)} positions:")
    for s in sign_flipped[:30]:
        log(f"    @+{s['offset']:5d}: L={s['L']:.6g} R={s['R']:.6g}")

    # =================================================================
    # 3. Float positions differing by IPD-scale amount (1 mm to 1 m)
    # =================================================================
    log("\n=== 3. IPD-scale differences (1mm - 1m, candidate per-eye offsets) ===")
    ipd_scale = []
    for i in range(N):
        lf, = struct.unpack("<f", l_data[i * 4:(i + 1) * 4])
        rf, = struct.unpack("<f", r_data[i * 4:(i + 1) * 4])
        if lf == rf: continue
        if abs(lf) < 1.0 or abs(rf) < 1.0: continue  # require non-tiny
        delta = abs(lf - rf)
        # IPD typical = 6cm = 0.06 in UE units (1 unit = 1cm in SN2)
        if 0.001 < delta < 100.0:
            ipd_scale.append({"offset": i * 4, "L": lf, "R": rf, "delta": rf - lf})
    log(f"  found {len(ipd_scale)} positions with IPD-magnitude differences:")
    for s in ipd_scale[:40]:
        log(f"    @+{s['offset']:5d}: L={s['L']:.6f} R={s['R']:.6f}  Δ={s['delta']:.6f}")

    # =================================================================
    # 4. Large-magnitude camera-position-like values that differ
    # =================================================================
    log("\n=== 4. LARGE-magnitude positional candidates (>100, differ small) ===")
    large_candidates = []
    for i in range(N - 3):
        # Try interpreting as float3 (3 consecutive floats)
        try:
            x_l, y_l, z_l = struct.unpack("<3f", l_data[i * 4:(i + 3) * 4])
            x_r, y_r, z_r = struct.unpack("<3f", r_data[i * 4:(i + 3) * 4])
        except Exception:
            continue
        # Require non-zero magnitudes
        mag_l = abs(x_l) + abs(y_l) + abs(z_l)
        mag_r = abs(x_r) + abs(y_r) + abs(z_r)
        if mag_l < 100 and mag_r < 100: continue
        # Require small per-component differences (IPD-scale)
        diff_x = abs(x_r - x_l)
        diff_y = abs(y_r - y_l)
        diff_z = abs(z_r - z_l)
        max_diff = max(diff_x, diff_y, diff_z)
        if not (0.001 < max_diff < 100): continue
        # Require not all the same
        if x_l == x_r and y_l == y_r and z_l == z_r: continue
        large_candidates.append({
            "offset": i * 4,
            "L": [x_l, y_l, z_l], "R": [x_r, y_r, z_r],
            "diff": [diff_x, diff_y, diff_z],
        })
    log(f"  {len(large_candidates)} large-magnitude positional candidates")
    for c in large_candidates[:30]:
        log(f"    @+{c['offset']:5d}: L={c['L']} R={c['R']} diff={c['diff']}")

    # =================================================================
    # 5. Pixel-history at multiple cave-opening candidate pixels
    # =================================================================
    log("\n\n=== 5. Pixel history at multiple test pixels ===")
    bb_id = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == "ResourceId::140984":
            bb_id = r.resourceId; break
    # Sample pixels across the cave-opening regions
    pixels_to_test = [
        ("LEFT_upper",   316, 100),
        ("LEFT_center",  316, 350),
        ("LEFT_lower",   316, 600),
        ("RIGHT_upper",  947, 100),
        ("RIGHT_center", 947, 350),
        ("RIGHT_lower",  947, 600),
    ]
    history_results = {}
    for label, px, py in pixels_to_test:
        try:
            history = controller.PixelHistory(
                bb_id, int(px), int(py),
                rd.Subresource(0, 0, 0),
                rd.CompType.UNorm,
            )
            log(f"\n  {label} ({px},{py}): {len(history)} write events")
            entries = []
            for ev in history[-6:]:
                eid = int(ev.eventId)
                try:
                    controller.SetFrameEvent(eid, True)
                    pipe = controller.GetPipelineState()
                    refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                    entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                    h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))[:16] if refl and len(refl.rawBytes) > 0 else None
                    d3d12 = controller.GetD3D12PipelineState()
                    pso = res_id(d3d12.pipelineResourceId)
                except Exception:
                    entry = None; h = None; pso = None
                try:
                    post = [ev.postMod.col.floatValue[i] for i in range(4)]
                except Exception:
                    post = None
                log(f"    eid {eid:5} PS={entry or '?':28} hash={h} PSO={pso}  post={post}")
                entries.append({"eventId": eid, "psEntry": entry, "psHash": h, "pso": pso, "post": post})
            history_results[label] = entries
        except Exception as e:
            log(f"  {label} pixel-history failed: {e}")

    with open(os.path.join(OUT_DIR, "underwater_flag.json"), "w") as f:
        json.dump({
            "zero_vs_nonzero": zero_vs_nonzero,
            "sign_flipped": sign_flipped,
            "ipd_scale": ipd_scale[:100],
            "large_candidates": large_candidates[:50],
            "history": history_results,
        }, f, indent=2, default=str)
    log("\nwrote underwater_flag.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
