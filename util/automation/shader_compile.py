"""Shader compilation wrappers — dxc.exe (HLSL→DXBC/DXIL) and glslang.exe
(GLSL→SPIR-V). Both are common dependencies for shader-patch validation.

Auto-discovery:
  1. Explicit path passed by caller
  2. RDOC_DXC / RDOC_GLSLANG env vars
  3. Windows SDK / Vulkan SDK install paths
  4. PATH lookup

Usage from Python:
    from util.automation import shader_compile
    out = shader_compile.compile_hlsl(
        source="...",
        entry="MainPS",
        target="ps_6_6",
        defines={"FOO": "1"},
    )
    # out is { "ok": True, "bytecode": b"...", "stdout": "...", "stderr": "..." }

Usage from CLI:
    python util/automation/shader_compile.py hlsl input.hlsl --entry MainPS --target ps_6_6 -Fo out.cso
    python util/automation/shader_compile.py glsl input.frag --stage frag -o out.spv
    python util/automation/shader_compile.py disasm out.cso
    python util/automation/shader_compile.py doctor
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def _candidate_dxc_paths():
    return [
        os.environ.get("RDOC_DXC"),
        shutil.which("dxc"),
        shutil.which("dxc.exe"),
        # Windows SDK
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\dxc.exe",
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\dxc.exe",
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22000.0\x64\dxc.exe",
        r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\dxc.exe",
        # Vulkan SDK
        r"C:\VulkanSDK\1.4.321.1\Bin\dxc.exe",
        r"C:\VulkanSDK\1.3.290.0\Bin\dxc.exe",
    ]


def _candidate_glslang_paths():
    return [
        os.environ.get("RDOC_GLSLANG"),
        shutil.which("glslang"),
        shutil.which("glslangValidator"),
        shutil.which("glslangValidator.exe"),
        r"C:\VulkanSDK\1.4.321.1\Bin\glslangValidator.exe",
        r"C:\VulkanSDK\1.3.290.0\Bin\glslangValidator.exe",
    ]


def find_dxc() -> str:
    for p in _candidate_dxc_paths():
        if p and os.path.isfile(p):
            return p
    return None


def find_glslang() -> str:
    for p in _candidate_glslang_paths():
        if p and os.path.isfile(p):
            return p
    return None


def compile_hlsl(source: str = None, source_path: str = None,
                 entry: str = "main", target: str = "ps_6_6",
                 defines: dict = None, include_dirs: list = None,
                 extra_args: list = None, dxc_path: str = None,
                 spirv: bool = False, output_path: str = None) -> dict:
    """Compile HLSL source → DXBC/DXIL (or SPIR-V if spirv=True).

    Returns:
      { "ok": bool, "exit_code": int, "stdout": str, "stderr": str,
        "bytecode": bytes | None, "output_path": str | None,
        "dxc_path": str, "cmd": [...] }
    """
    dxc = dxc_path or find_dxc()
    if not dxc:
        return {"ok": False, "error": "dxc.exe not found — set RDOC_DXC env var"}
    tmp_source = None
    if source_path is None:
        if source is None:
            return {"ok": False, "error": "must provide source or source_path"}
        f = tempfile.NamedTemporaryFile(suffix=".hlsl", delete=False, mode="w")
        f.write(source); f.close()
        tmp_source = f.name
        source_path = tmp_source
    out_path = output_path or (source_path + (".spv" if spirv else ".cso"))
    cmd = [dxc, "-E", entry, "-T", target, "-Fo", out_path]
    if spirv:
        cmd += ["-spirv"]
    if defines:
        for k, v in defines.items():
            cmd += ["-D", f"{k}={v}"]
    if include_dirs:
        for d in include_dirs:
            cmd += ["-I", d]
    if extra_args:
        cmd += list(extra_args)
    cmd += [source_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        if tmp_source and os.path.exists(tmp_source):
            try: os.unlink(tmp_source)
            except Exception: pass
    bytecode = None
    if result.returncode == 0 and os.path.exists(out_path):
        with open(out_path, "rb") as bf:
            bytecode = bf.read()
        if not output_path:
            # caller didn't ask for a file — clean up
            try: os.unlink(out_path)
            except Exception: pass
            out_path = None
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "bytecode": bytecode,
        "output_path": out_path,
        "dxc_path": dxc,
        "cmd": cmd,
    }


def compile_glsl(source: str = None, source_path: str = None,
                 stage: str = None, output_path: str = None,
                 glslang_path: str = None, extra_args: list = None) -> dict:
    """Compile GLSL → SPIR-V via glslangValidator.

    stage: 'vert', 'frag', 'comp', 'tesc', 'tese', 'geom', 'rgen', 'rint',
           'rahit', 'rchit', 'rmiss', 'rcall', 'task', 'mesh'.
    """
    glslang = glslang_path or find_glslang()
    if not glslang:
        return {"ok": False, "error": "glslangValidator.exe not found — install Vulkan SDK or set RDOC_GLSLANG"}
    tmp_source = None
    if source_path is None:
        if source is None:
            return {"ok": False, "error": "must provide source or source_path"}
        suffix = f".{stage or 'frag'}"
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w")
        f.write(source); f.close()
        tmp_source = f.name
        source_path = tmp_source
    out_path = output_path or (source_path + ".spv")
    cmd = [glslang, "-V", "-o", out_path]
    if stage:
        cmd += ["-S", stage]
    if extra_args:
        cmd += list(extra_args)
    cmd += [source_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        if tmp_source and os.path.exists(tmp_source):
            try: os.unlink(tmp_source)
            except Exception: pass
    bytecode = None
    if result.returncode == 0 and os.path.exists(out_path):
        with open(out_path, "rb") as bf:
            bytecode = bf.read()
        if not output_path:
            try: os.unlink(out_path)
            except Exception: pass
            out_path = None
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "bytecode": bytecode,
        "output_path": out_path,
        "glslang_path": glslang,
        "cmd": cmd,
    }


def disassemble_dxbc(bytecode: bytes = None, path: str = None,
                     dxc_path: str = None) -> dict:
    """Disassemble a DXBC/DXIL blob via `dxc -dumpbin`."""
    dxc = dxc_path or find_dxc()
    if not dxc:
        return {"ok": False, "error": "dxc.exe not found"}
    tmp_input = None
    if path is None:
        if bytecode is None:
            return {"ok": False, "error": "must provide bytecode or path"}
        f = tempfile.NamedTemporaryFile(suffix=".dxbc", delete=False, mode="wb")
        f.write(bytecode); f.close()
        tmp_input = f.name
        path = tmp_input
    cmd = [dxc, "-dumpbin", path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        if tmp_input and os.path.exists(tmp_input):
            try: os.unlink(tmp_input)
            except Exception: pass
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "dxc_path": dxc,
        "cmd": cmd,
    }


def doctor() -> dict:
    """Report whether each shader-compile tool is wired up + which path is in use."""
    return {
        "dxc": {
            "found": find_dxc(),
            "candidates": [p for p in _candidate_dxc_paths() if p],
        },
        "glslang": {
            "found": find_glslang(),
            "candidates": [p for p in _candidate_glslang_paths() if p],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_hlsl = sub.add_parser("hlsl", help="compile HLSL → DXBC/DXIL/SPIR-V")
    p_hlsl.add_argument("source")
    p_hlsl.add_argument("--entry", default="main")
    p_hlsl.add_argument("--target", default="ps_6_6")
    p_hlsl.add_argument("--define", "-D", action="append", default=[])
    p_hlsl.add_argument("--include", "-I", action="append", default=[])
    p_hlsl.add_argument("--spirv", action="store_true")
    p_hlsl.add_argument("--output", "-Fo")
    p_hlsl.add_argument("--extra-arg", action="append", default=[])

    p_glsl = sub.add_parser("glsl", help="compile GLSL → SPIR-V")
    p_glsl.add_argument("source")
    p_glsl.add_argument("--stage", "-S")
    p_glsl.add_argument("--output", "-o")

    p_dis = sub.add_parser("disasm", help="disassemble DXBC/DXIL via dxc -dumpbin")
    p_dis.add_argument("input")

    sub.add_parser("doctor", help="report tool discovery state")

    args = ap.parse_args()

    if args.cmd == "doctor":
        import json
        print(json.dumps(doctor(), indent=2))
        return 0
    if args.cmd == "hlsl":
        defines = dict(d.split("=", 1) if "=" in d else (d, "1") for d in args.define)
        r = compile_hlsl(source_path=args.source, entry=args.entry,
                          target=args.target, defines=defines,
                          include_dirs=args.include, spirv=args.spirv,
                          output_path=args.output, extra_args=args.extra_arg)
        print(r.get("stdout", ""))
        print(r.get("stderr", ""), file=sys.stderr)
        return 0 if r.get("ok") else 1
    if args.cmd == "glsl":
        r = compile_glsl(source_path=args.source, stage=args.stage,
                          output_path=args.output)
        print(r.get("stdout", ""))
        print(r.get("stderr", ""), file=sys.stderr)
        return 0 if r.get("ok") else 1
    if args.cmd == "disasm":
        r = disassemble_dxbc(path=args.input)
        print(r.get("stdout", ""))
        print(r.get("stderr", ""), file=sys.stderr)
        return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
