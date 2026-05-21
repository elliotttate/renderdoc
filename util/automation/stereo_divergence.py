"""Automatic stereo-bug divergence top-N ranking.

For every matched left/right event pair (via ``pair_eye_events``), compute
a composite divergence score from cheap-to-collect signals, then rank
descending. The top results are where to look.

Signals contributing to the score:

  - RT bound resource ID delta (categorical: 0 or 1)
  - RT min/max delta from ``GetMinMax`` (HDR-safe)
  - PSO ID delta (categorical: 0 or 1)
  - Bound shader hash delta per stage (one per differing stage)
  - Bound SRV/CBV ResourceId delta per register
  - Workgroup-count delta (for dispatches)
  - Cbuffer byte delta on root parameter 0 (typically the View cbuffer)

The score is a weighted sum; weights are deliberately conservative so
they surface real divergences rather than noise (e.g., a single SRV
binding diff is worth more than a 1% RT pixel delta).

Usage::

    python -m util.automation.stereo_divergence <cap.rdc> [--top 10] [--out file]
"""

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import eye_classifier  # type: ignore
    from automation import pair_eye_events  # type: ignore
else:
    from . import _lib, eye_classifier, pair_eye_events

import renderdoc as rd  # noqa: E402


# Weight for each contributing signal. Tuned so SRV binding differences
# dominate (since that's the most common cause of stereo bugs), with HDR
# min/max contributing meaningfully but not swamping.
W_RT_RESOURCE_DELTA = 5.0
W_RT_MIN_MAX_DELTA = 1.0  # multiplied by per-channel L2 distance
W_PSO_DELTA = 3.0
W_SHADER_HASH_DELTA = 4.0   # per differing stage
W_SRV_BINDING_DELTA = 2.5   # per differing register
W_CBV_BINDING_DELTA = 2.0
W_DISPATCH_DIMENSION_DELTA = 3.0
W_CB0_BYTE_DELTA = 0.5      # multiplied by min(byte_diff_count, 32) / 32


def _safe_minmax(controller, rt_resource, sub) -> Optional[Tuple[List[float], List[float]]]:
    try:
        mn, mx = controller.GetMinMax(rt_resource, sub, rd.CompType.Typeless)
        return ([float(mn.floatValue[c]) for c in range(4)],
                [float(mx.floatValue[c]) for c in range(4)])
    except Exception:
        return None


def _bindings_dict(state, stage_filter: Optional[str] = None) -> Dict[Tuple[str, str, Any], Dict[str, Any]]:
    out: Dict[Tuple[str, str, Any], Dict[str, Any]] = {}
    for b in state.get("bindings", []):
        if stage_filter and b.get("stage") != stage_filter:
            continue
        key = (b.get("stage"), b.get("type"), b.get("register"))
        out[key] = b
    return out


def _score_pair(controller, left_eid: int, right_eid: int,
                cb0_hashes: Dict[int, str]) -> Dict[str, Any]:
    """Compute the divergence score + breakdown for a single (left, right) pair."""
    left_state = _lib.collect_state_at_event(controller, left_eid)
    right_state = _lib.collect_state_at_event(controller, right_eid)

    breakdown: Dict[str, Any] = {}
    score = 0.0

    # PSO ID
    pso_l = left_state.get("pipelineId")
    pso_r = right_state.get("pipelineId")
    if pso_l != pso_r:
        score += W_PSO_DELTA
        breakdown["pipelineIdDelta"] = (pso_l, pso_r)

    # Shader hashes per stage
    sh_l = {s["stage"]: s.get("bytecodeHash") for s in left_state.get("shaders", [])}
    sh_r = {s["stage"]: s.get("bytecodeHash") for s in right_state.get("shaders", [])}
    diff_stages = []
    for stage in sorted(set(sh_l.keys()) | set(sh_r.keys())):
        if sh_l.get(stage) != sh_r.get(stage):
            score += W_SHADER_HASH_DELTA
            diff_stages.append({"stage": stage, "left": sh_l.get(stage), "right": sh_r.get(stage)})
    if diff_stages:
        breakdown["shaderHashDeltas"] = diff_stages

    # SRV / CBV bindings
    bind_l = _bindings_dict(left_state)
    bind_r = _bindings_dict(right_state)
    keys = sorted(set(bind_l.keys()) | set(bind_r.keys()), key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    binding_deltas = []
    for k in keys:
        bl = bind_l.get(k) or {}
        br = bind_r.get(k) or {}
        rl = bl.get("resource")
        rr = br.get("resource")
        if rl != rr:
            stage, btype, reg = k
            is_srv = (btype or "").startswith("Read")
            weight = W_SRV_BINDING_DELTA if is_srv else W_CBV_BINDING_DELTA
            score += weight
            binding_deltas.append({
                "stage": stage, "type": btype, "register": reg,
                "left": rl, "right": rr,
            })
    if binding_deltas:
        breakdown["bindingDeltas"] = binding_deltas

    # Render-target deltas
    rts_l = left_state.get("renderTargets", []) or []
    rts_r = right_state.get("renderTargets", []) or []
    rt_resource_delta = False
    for i in range(min(len(rts_l), len(rts_r))):
        if (rts_l[i].get("resource") != rts_r[i].get("resource")) or \
                (rts_l[i].get("view") != rts_r[i].get("view")):
            rt_resource_delta = True
            break
    if rt_resource_delta:
        score += W_RT_RESOURCE_DELTA
        breakdown["rtResourceDelta"] = True

    # RT min/max delta (only if same RT resource)
    if rts_l and rts_r and rts_l[0].get("resource") == rts_r[0].get("resource"):
        rid = rts_l[0]["resource"]
        # find textures matching rid
        try:
            for t in controller.GetTextures():
                if str(t.resourceId) == rid:
                    sub_l = rd.Subresource(int(rts_l[0].get("firstMip", 0)),
                                            int(rts_l[0].get("firstSlice", 0)), 0)
                    sub_r = rd.Subresource(int(rts_r[0].get("firstMip", 0)),
                                            int(rts_r[0].get("firstSlice", 0)), 0)
                    controller.SetFrameEvent(left_eid, True)
                    mm_l = _safe_minmax(controller, t.resourceId, sub_l)
                    controller.SetFrameEvent(right_eid, True)
                    mm_r = _safe_minmax(controller, t.resourceId, sub_r)
                    if mm_l and mm_r:
                        delta = 0.0
                        for c in range(4):
                            d_min = abs(mm_r[0][c] - mm_l[0][c])
                            d_max = abs(mm_r[1][c] - mm_l[1][c])
                            delta += (d_min + d_max) * 0.5
                        score += W_RT_MIN_MAX_DELTA * delta
                        breakdown["rtMinMaxDelta"] = delta
                    break
        except Exception:
            pass

    # CB0 byte delta
    if left_eid in cb0_hashes and right_eid in cb0_hashes:
        if cb0_hashes[left_eid] != cb0_hashes[right_eid]:
            score += W_CB0_BYTE_DELTA * 16.0  # different hash → assume significant
            breakdown["cb0HashDelta"] = (cb0_hashes[left_eid], cb0_hashes[right_eid])

    return {"score": score, "breakdown": breakdown}


def _action_dispatch_dim(action) -> Optional[List[int]]:
    try:
        return [int(v) for v in action.dispatchDimension]
    except Exception:
        return None


def rank(capture_path: str, top_n: int = 10) -> Dict[str, Any]:
    """Compute divergence scores for every matched eye pair and return top N."""
    # Eye classify
    eye = eye_classifier.classify_capture(capture_path, {"mode": "auto"})
    by_eid = {int(e["eventId"]): e for e in eye["events"]}

    # Pair events
    try:
        pairs_info = pair_eye_events.pair_events(capture_path)
    except AttributeError:
        # Older variant
        pairs_info = {"pairs": []}
    pairs = pairs_info.get("pairs", [])

    cap, controller = _lib.open_capture(capture_path)
    try:
        # Precompute CB0 byte-hash per relevant event (best-effort, may fail
        # for events without a bound CBV). 16-byte head hash is enough.
        cb0_hashes: Dict[int, str] = {}
        candidate_eids = set()
        for p in pairs:
            # pair_eye_events returns {"left": <int eid>, "right": <int eid>, ...}
            # — handle both the int shape and a (legacy) {"eventId": ...} dict shape.
            l = p["left"]
            r = p["right"]
            candidate_eids.add(int(l["eventId"]) if isinstance(l, dict) else int(l))
            candidate_eids.add(int(r["eventId"]) if isinstance(r, dict) else int(r))
        for eid in sorted(candidate_eids):
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                arr = pipe.GetConstantBlocks(rd.ShaderStage.Pixel, False)
                if arr is not None and len(arr) > 0:
                    used = arr[0]
                    desc = used.descriptor
                    if desc is not None and desc.resource is not None:
                        try:
                            data = bytes(controller.GetBufferData(desc.resource, int(desc.byteOffset), min(int(desc.byteSize) or 256, 256)))
                            cb0_hashes[eid] = hashlib.md5(data).hexdigest()[:16]
                        except Exception:
                            pass
            except Exception:
                continue

        # Action dispatchDimension lookup
        action_by_eid: Dict[int, Any] = {}
        for a in _lib.walk_actions(controller):
            action_by_eid[int(a.eventId)] = a

        # Score every pair
        scored = []
        for p in pairs:
            l = p["left"]; r = p["right"]
            left_eid = int(l["eventId"]) if isinstance(l, dict) else int(l)
            right_eid = int(r["eventId"]) if isinstance(r, dict) else int(r)

            try:
                result = _score_pair(controller, left_eid, right_eid, cb0_hashes)
            except Exception as exc:
                result = {"score": 0.0, "breakdown": {"error": str(exc)}}

            # Dispatch dim delta
            la = action_by_eid.get(left_eid)
            ra = action_by_eid.get(right_eid)
            if la is not None and ra is not None:
                dl = _action_dispatch_dim(la)
                dr = _action_dispatch_dim(ra)
                if dl and dr and dl != dr:
                    result["score"] = result.get("score", 0.0) + W_DISPATCH_DIMENSION_DELTA
                    result.setdefault("breakdown", {})["dispatchDimensionDelta"] = (dl, dr)
                    if all(v == 0 for v in dr) and not all(v == 0 for v in dl):
                        result["breakdown"]["rightDispatchDead"] = True
                        # double-weight the dead-dispatch signal: it's almost always the bug
                        result["score"] += W_DISPATCH_DIMENSION_DELTA * 2

            scored.append({
                "leftEventId": left_eid,
                "rightEventId": right_eid,
                "score": float(result.get("score", 0.0)),
                "breakdown": result.get("breakdown", {}),
            })

        scored.sort(key=lambda r: r["score"], reverse=True)
        return {
            "pairCount": len(pairs),
            "scoredPairs": len(scored),
            "top": scored[:top_n],
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Top-N stereo divergence ranking.")
    p.add_argument("capture")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = rank(args.capture, top_n=args.top)
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
