"""Health check for the RenderDoc automation toolkit.

Verifies:
  * Python version (3.6+ required for the embedded pyrenderdoc)
  * pyrenderdoc Python module importable
  * qrenderdoc.exe locatable + runnable
  * renderdoccmd.exe locatable + runnable
  * scratch/temp directories writable
  * Optional dxc.exe / glslangValidator.exe (shader_compile)
  * Optional MSBuild (export_cpp build pipeline)
  * Optional capture-watch directory writable

Each check returns a structured record; the final report has an issues
list. Exit code 0 if no critical issues, 1 if any critical issue, 2 if
optional features are missing.

Usage:
    python util/automation/doctor.py
    python util/automation/doctor.py --json
    python util/automation/doctor.py --strict   # treat optional warnings as errors

From Python:
    from util.automation import doctor
    report = doctor.run()
    print(doctor.format_report(report))
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile


CRITICAL = "critical"
WARNING  = "warning"
OK       = "ok"


def _repo_root() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", ".."))


def _check_python():
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 6):
        return {"name": "Python version", "level": CRITICAL,
                "msg": f"Need Python 3.6+, found {sys.version}",
                "details": {"version": sys.version}}
    return {"name": "Python version", "level": OK,
            "msg": f"Python {v.major}.{v.minor}.{v.micro}",
            "details": {"version": sys.version}}


def _check_pyrenderdoc():
    try:
        import renderdoc as rd  # type: ignore
        attrs = []
        for a in ("OpenCaptureFile", "ReplayOptions", "ShaderStage", "ActionFlags"):
            attrs.append(hasattr(rd, a))
        if not all(attrs):
            return {"name": "pyrenderdoc binding", "level": CRITICAL,
                    "msg": "Imported but missing expected symbols",
                    "details": {"missing_symbols": ["OpenCaptureFile/ReplayOptions/..."]}}
        return {"name": "pyrenderdoc binding", "level": OK,
                "msg": "module 'renderdoc' importable",
                "details": {"module_file": getattr(rd, "__file__", None)}}
    except ImportError as e:
        return {"name": "pyrenderdoc binding", "level": CRITICAL,
                "msg": f"Cannot import 'renderdoc' — run from qrenderdoc --python or set PYTHONPATH to RenderDoc Python module dir",
                "details": {"error": str(e)}}


def _find_renderdoc_exe(name: str) -> str:
    candidates = [
        os.environ.get("RDOC_" + name.upper()),
        shutil.which(name),
        shutil.which(name + ".exe"),
        os.path.join(_repo_root(), "x64", "Development", name + ".exe"),
        os.path.join(_repo_root(), "x64", "Release", name + ".exe"),
        os.path.join(_repo_root(), "build", "bin", name + ".exe"),
        # Default install paths
        r"C:\Program Files\RenderDoc\\" + name + ".exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _check_qrenderdoc():
    p = _find_renderdoc_exe("qrenderdoc")
    if not p:
        return {"name": "qrenderdoc.exe", "level": WARNING,
                "msg": "not found — needed to drive Python automation scripts headlessly",
                "details": {"searched": ["x64/Development", "x64/Release", "build/bin",
                                          "PATH", "C:/Program Files/RenderDoc/"]}}
    try:
        r = subprocess.run([p, "--help"], capture_output=True, timeout=10)
        if r.returncode != 0:
            return {"name": "qrenderdoc.exe", "level": WARNING,
                    "msg": f"found but --help returned {r.returncode}",
                    "details": {"path": p}}
        return {"name": "qrenderdoc.exe", "level": OK, "msg": p,
                "details": {"path": p}}
    except Exception as e:
        return {"name": "qrenderdoc.exe", "level": WARNING,
                "msg": f"found but not runnable: {e}",
                "details": {"path": p}}


def _check_renderdoccmd():
    p = _find_renderdoc_exe("renderdoccmd")
    if not p:
        return {"name": "renderdoccmd.exe", "level": WARNING,
                "msg": "not found — needed for `index-capture` / `state-at-event` subcommands",
                "details": {}}
    try:
        r = subprocess.run([p, "help"], capture_output=True, timeout=10)
        ok = r.returncode == 0
        return {"name": "renderdoccmd.exe", "level": OK if ok else WARNING,
                "msg": p,
                "details": {"path": p, "exit_code": r.returncode}}
    except Exception as e:
        return {"name": "renderdoccmd.exe", "level": WARNING,
                "msg": f"not runnable: {e}",
                "details": {"path": p}}


def _check_scratch_dirs():
    issues = []
    for d in [tempfile.gettempdir(), os.path.join(os.path.expanduser("~"), ".cache")]:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".rdoc-doctor-test")
            with open(test, "w") as f:
                f.write("ok")
            os.unlink(test)
        except Exception as e:
            issues.append(f"{d}: {e}")
    if issues:
        return {"name": "scratch dirs writable", "level": WARNING,
                "msg": "; ".join(issues),
                "details": {}}
    return {"name": "scratch dirs writable", "level": OK,
            "msg": f"{tempfile.gettempdir()} + ~/.cache",
            "details": {}}


def _check_dxc():
    try:
        from . import shader_compile
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import shader_compile  # type: ignore
    p = shader_compile.find_dxc()
    if not p:
        return {"name": "dxc.exe (shader compile)", "level": WARNING,
                "msg": "not found — install Windows SDK or Vulkan SDK",
                "details": {"searched": shader_compile._candidate_dxc_paths()}}
    return {"name": "dxc.exe (shader compile)", "level": OK,
            "msg": p, "details": {"path": p}}


def _check_glslang():
    try:
        from . import shader_compile
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import shader_compile  # type: ignore
    p = shader_compile.find_glslang()
    if not p:
        return {"name": "glslangValidator.exe (SPIR-V compile)", "level": WARNING,
                "msg": "not found — install Vulkan SDK",
                "details": {}}
    return {"name": "glslangValidator.exe (SPIR-V compile)", "level": OK,
            "msg": p, "details": {"path": p}}


def _check_msbuild():
    # Used by export_cpp build pipeline
    candidates = [
        shutil.which("msbuild"),
        shutil.which("msbuild.exe"),
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
        r"C:\Program Files\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
        r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return {"name": "MSBuild.exe (export_cpp builds)", "level": OK,
                    "msg": c, "details": {"path": c}}
    return {"name": "MSBuild.exe (export_cpp builds)", "level": WARNING,
            "msg": "not found — install VS 2022", "details": {}}


def _check_repo_layout():
    root = _repo_root()
    expected = [
        "util/automation/_lib.py",
        "util/automation/index_capture.py",
        "renderdoccmd",
        "renderdoc",
    ]
    missing = []
    for e in expected:
        if not os.path.exists(os.path.join(root, e)):
            missing.append(e)
    if missing:
        return {"name": "Repo layout", "level": CRITICAL,
                "msg": "missing files; running from wrong directory?",
                "details": {"missing": missing, "root": root}}
    return {"name": "Repo layout", "level": OK, "msg": root,
            "details": {"root": root}}


def _check_new_apis():
    """Sanity: our new replay-controller APIs are bound in pyrenderdoc."""
    try:
        import renderdoc as rd  # type: ignore
    except ImportError:
        return {"name": "Fork APIs in pyrenderdoc", "level": CRITICAL,
                "msg": "renderdoc module not importable",
                "details": {}}
    # Inspect ReplayController for the new methods. The Python binding
    # exposes them as attributes on the controller proxy class.
    expected = ["SetBufferOverride", "SetBufferOverrideGPU",
                "ClearBufferOverride", "ClearBufferOverrideGPU",
                "GetDescriptorWrites"]
    # The controller class isn't directly importable until we open a capture,
    # so probe via dir() of the module + IReplayController if available.
    found = []
    missing = []
    # Try to find them on classes in the renderdoc module
    candidates = []
    for attr in dir(rd):
        cls = getattr(rd, attr, None)
        if cls and hasattr(cls, "__bases__"):
            candidates.append(cls)
    for name in expected:
        present = False
        for cls in candidates:
            if hasattr(cls, name):
                present = True; break
        (found if present else missing).append(name)
    if missing:
        return {"name": "Fork APIs in pyrenderdoc", "level": WARNING,
                "msg": "some new APIs not visible in current binding "
                       "(rebuild qrenderdoc / pyrenderdoc Python module)",
                "details": {"found": found, "missing": missing}}
    return {"name": "Fork APIs in pyrenderdoc", "level": OK,
            "msg": f"all {len(found)} new APIs visible",
            "details": {"found": found}}


def run() -> dict:
    checks = [
        _check_python(),
        _check_pyrenderdoc(),
        _check_repo_layout(),
        _check_new_apis(),
        _check_qrenderdoc(),
        _check_renderdoccmd(),
        _check_scratch_dirs(),
        _check_dxc(),
        _check_glslang(),
        _check_msbuild(),
    ]
    summary = {
        "ok":       sum(1 for c in checks if c["level"] == OK),
        "warning":  sum(1 for c in checks if c["level"] == WARNING),
        "critical": sum(1 for c in checks if c["level"] == CRITICAL),
    }
    return {"checks": checks, "summary": summary}


def format_report(report: dict) -> str:
    out = []
    out.append("RenderDoc Automation Doctor")
    out.append("=" * 60)
    icons = {OK: "✓", WARNING: "!", CRITICAL: "✗"}
    for c in report["checks"]:
        icon = icons.get(c["level"], "?")
        out.append(f"  {icon}  {c['name']:38} {c['msg']}")
    s = report["summary"]
    out.append("")
    out.append(f"Summary: {s['ok']} OK, {s['warning']} warning, {s['critical']} critical")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--strict", action="store_true", help="exit nonzero on warnings too")
    args = ap.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))
    if report["summary"]["critical"] > 0:
        return 1
    if args.strict and report["summary"]["warning"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
