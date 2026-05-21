"""UEVR ingest and correlation (§7, §17 of the roadmap).

Reads sidecar JSON dropped by UEVR (override manifest, runtime status, eye
sample dumps, D3D12 diagnostic snapshots, hook-mode flags) and attaches eye +
override metadata to events in an existing ``index_capture`` output. Produces a
``uevr.jsonl`` alongside the index that can be joined to ``state.jsonl`` by
``eventId``.

Files this knows about (any subset is OK)
-----------------------------------------

``manifest.json``
    ``{"overrides": [{"target_hash": "166dba88", "replacement_hash": "abcd...",
                      "transform_ops": 12, "enabled": true,
                      "reload_timestamp": "2025-12-01T12:00:00Z"}, ...]}``

``status.json``
    ``{"profile": "...", "uevr_active": true, "hook_mode": "frontend",
       "stereo_mode": "sbs", "swap_eyes": false}``

``events.json``
    ``{"events": [{"eventId": 16042, "eye": "right", "tag": "magenta_probe"}, ...]}``

``build.json``
    ``{"exe": "...", "cmdline": "...", "build": "..."}``

``eye_samples.json[l]``
    Either ``{"samples": [...]}`` or one JSON object per line. Each sample is::

        {
            "frameIndex": 4732,
            "eye": "right",
            "viewMatrix": [...16 floats...],
            "projMatrix": [...16 floats...],
            "fov": [hfov, vfov],
            "ipd": 0.064,
            "stereoPass": "Render_Right",
            "eventId": 16042
        }

    UEVR drops these from the runtime as it inspects each draw. ``eventId`` is
    optional — if missing we'll correlate by ``frameIndex`` and ``eye`` against
    the events we already classified.

``diag.json``
    D3D12 diagnostic snapshot. Schema is intentionally loose — we forward any
    fields verbatim and surface known keys (descriptor heap state, ExecuteIndirect
    diagnostics, pipeline-stream PSO warnings, hook errors) in the summary::

        {
            "descriptor_heaps": [{"id": "...", "type": "CBV_SRV_UAV", "size": 1000000}],
            "execute_indirect": {"signatures": [...], "unsupported": false},
            "pipeline_stream_psos": {"observed": 423, "unsupported": 0},
            "hook_errors": [],
            "warnings": ["pipeline stream PSO X used by Y but unrecognised"]
        }

Usage
-----

::

    python -m util.automation.uevr_ingest <index_dir> --uevr-dir <dir-with-jsons>

If your UEVR run dropped JSONL instead of JSON for the larger files, this
module reads both transparently.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Sidecar readers
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Optional[Any]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: str) -> List[Any]:
    out: List[Any] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _read_either(uevr_dir: str, basename: str) -> Any:
    """Try ``<basename>.json``, then ``<basename>.jsonl``. JSONL becomes a list."""
    j = _read_json(os.path.join(uevr_dir, f"{basename}.json"))
    if j is not None:
        return j
    rows = _read_jsonl(os.path.join(uevr_dir, f"{basename}.jsonl"))
    return rows if rows else None


# ---------------------------------------------------------------------------
# Eye sample correlation
# ---------------------------------------------------------------------------

def _normalise_samples(raw: Any) -> List[Dict[str, Any]]:
    """Accept either ``{"samples": [...]}``, a bare list, or a JSONL row stream."""
    if raw is None:
        return []
    if isinstance(raw, dict) and "samples" in raw:
        return list(raw["samples"])
    if isinstance(raw, list):
        return list(raw)
    return []


def _correlate_samples_to_events(
    samples: List[Dict[str, Any]],
    state_rows: List[Dict[str, Any]],
    classified_events: Dict[int, str],
) -> Dict[int, Dict[str, Any]]:
    """Return ``{eventId: sample}`` correlation.

    Priority: explicit ``eventId`` in the sample wins. Otherwise pair by
    (frameIndex, eye) against the events the classifier already labelled.
    """
    correlated: Dict[int, Dict[str, Any]] = {}

    # Bucket fallback samples by (frameIndex, eye).
    by_frame_eye: Dict[tuple, List[Dict[str, Any]]] = {}
    for s in samples:
        if "eventId" in s and s["eventId"] is not None:
            try:
                correlated[int(s["eventId"])] = s
            except Exception:
                pass
            continue
        key = (s.get("frameIndex"), (s.get("eye") or "").lower())
        if key[0] is None:
            continue
        by_frame_eye.setdefault(key, []).append(s)

    # Greedy match: for each classified event, look up the matching bucket and
    # pop the first sample. Captures usually have one frame so frameIndex is
    # typically 0, which means this falls back to per-eye order.
    bucket_cursors: Dict[tuple, int] = {}
    # We don't have frameIndex in the index, so assume the only frame is 0.
    default_frame = 0
    for eid, eye in classified_events.items():
        eye_lc = (eye or "").lower()
        key = (default_frame, eye_lc)
        bucket = by_frame_eye.get(key, [])
        i = bucket_cursors.get(key, 0)
        if i < len(bucket):
            correlated.setdefault(int(eid), bucket[i])
            bucket_cursors[key] = i + 1
    return correlated


# ---------------------------------------------------------------------------
# D3D12 diagnostic summary
# ---------------------------------------------------------------------------

def _summarise_diag(diag: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not diag:
        return {}
    out: Dict[str, Any] = {}
    if "descriptor_heaps" in diag:
        heaps = diag["descriptor_heaps"]
        if isinstance(heaps, list):
            out["descriptorHeapCount"] = len(heaps)
            out["descriptorHeapTotalSize"] = sum(int(h.get("size", 0) or 0) for h in heaps if isinstance(h, dict))
    if "execute_indirect" in diag:
        ei = diag["execute_indirect"] or {}
        out["executeIndirectSignatureCount"] = len(ei.get("signatures", []) or [])
        out["executeIndirectUnsupported"] = bool(ei.get("unsupported", False))
    if "pipeline_stream_psos" in diag:
        ps = diag["pipeline_stream_psos"] or {}
        out["pipelineStreamPSOsObserved"] = int(ps.get("observed", 0) or 0)
        out["pipelineStreamPSOsUnsupported"] = int(ps.get("unsupported", 0) or 0)
    if "hook_errors" in diag:
        out["hookErrorCount"] = len(diag["hook_errors"] or [])
    if "warnings" in diag:
        out["warningCount"] = len(diag["warnings"] or [])
        # Surface the first handful for quick inspection.
        out["warningSample"] = list((diag["warnings"] or [])[:5])
    return out


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------

def ingest(index_dir: str, uevr_dir: str) -> Dict[str, Any]:
    manifest = _read_json(os.path.join(uevr_dir, "manifest.json")) or {}
    status = _read_json(os.path.join(uevr_dir, "status.json")) or {}
    annotated_events = _read_json(os.path.join(uevr_dir, "events.json")) or {}
    build_info = _read_json(os.path.join(uevr_dir, "build.json")) or {}
    samples_raw = _read_either(uevr_dir, "eye_samples")
    samples = _normalise_samples(samples_raw)
    diag = _read_json(os.path.join(uevr_dir, "diag.json"))

    # Hash -> override entry
    override_by_target: Dict[str, Dict[str, Any]] = {}
    for o in manifest.get("overrides", []):
        h = o.get("target_hash")
        if h:
            override_by_target[h.lower()] = o

    # Index state
    state_rows = _read_jsonl(os.path.join(index_dir, "state.jsonl"))
    state = {s["eventId"]: s for s in state_rows}
    explicit_event_anno = {e["eventId"]: e for e in annotated_events.get("events", [])}

    # Existing classified events: pick eye from events.json if present.
    classified_events = {int(e["eventId"]): e.get("eye") for e in annotated_events.get("events", []) if "eventId" in e}

    sample_by_event = _correlate_samples_to_events(samples, state_rows, classified_events)

    rows: List[Dict[str, Any]] = []
    for eid in sorted(state.keys()):
        s = state[eid]
        shader_hashes = [sh.get("bytecodeHash") for sh in s.get("shaders", []) if sh.get("bytecodeHash")]
        active = []
        for h in shader_hashes:
            ov = override_by_target.get((h or "").lower())
            if ov is not None:
                active.append(ov)
        anno = explicit_event_anno.get(eid, {})
        sample = sample_by_event.get(eid)
        row = {
            "eventId": eid,
            "eye": anno.get("eye") or (sample.get("eye") if sample else None),
            "tag": anno.get("tag"),
            "boundShaderHashes": shader_hashes,
            "matchingOverrides": active,
            "override_expected": len(active) > 0,
        }
        if sample is not None:
            row["eyeSample"] = {
                k: sample.get(k)
                for k in ("frameIndex", "eye", "viewMatrix", "projMatrix", "fov", "ipd", "stereoPass")
                if k in sample
            }
        rows.append(row)

    overrides = manifest.get("overrides", [])
    summary = {
        "uevr_status": status,
        "build_info": build_info,
        "override_count": len(overrides),
        "override_enabled_count": sum(1 for o in overrides if o.get("enabled")),
        "events_with_matching_override": sum(1 for r in rows if r["override_expected"]),
        "eye_sample_count": len(samples),
        "eye_samples_correlated": len(sample_by_event),
        "d3d12_diag": _summarise_diag(diag),
    }
    return {"summary": summary, "rows": rows, "diag": diag or {}}


def write_uevr_jsonl(index_dir: str, ingest_result: Dict[str, Any], out_path: Optional[str] = None) -> str:
    out_path = out_path or os.path.join(index_dir, "uevr.jsonl")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            json.dumps({"_summary": ingest_result["summary"], "_diag": ingest_result.get("diag", {})}, ensure_ascii=False) + "\n"
        )
        for r in ingest_result["rows"]:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out_path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ingest UEVR sidecar JSON and correlate to an index.")
    p.add_argument("index_dir", help="Directory produced by index_capture")
    p.add_argument("--uevr-dir", required=True, help="Directory containing manifest.json/status.json/etc.")
    p.add_argument("--out", "-o", help="Output file (default: <index_dir>/uevr.jsonl)")
    args = p.parse_args(argv)

    result = ingest(args.index_dir, args.uevr_dir)
    out = write_uevr_jsonl(args.index_dir, result, args.out)
    print(json.dumps({"out": out, "summary": result["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
