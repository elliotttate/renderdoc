"""Deterministic launch profiles (§1 of the roadmap).

Provides a small profile schema + a launcher that uses the RenderDoc API to
inject and capture a target executable with reproducible flags.

Profile schema (JSON):
{
  "name": "SN2 clean emulatestereo",
  "exe": "<path>",
  "working_dir": "<path>",
  "args": "-emulatestereo -ResX=1280 -ResY=720 -windowed",
  "environment_mode": "clean" | "uevr" | "inherit",
  "uevr_profile": "<optional>",
  "capture_trigger": "hotkey" | "frame" | "delay",
  "capture_frame": 600,
  "capture_delay_seconds": 10.0,
  "out_dir": "<dir to save captures>",
  "output_name_prefix": "sn2_clean"
}

Usage:
    python -m util.automation.launch_profile run <profile.json>
    python -m util.automation.launch_profile validate <profile.json>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


REQUIRED_KEYS = ("name", "exe")


def validate(profile: dict) -> dict:
    issues = []
    for k in REQUIRED_KEYS:
        if k not in profile:
            issues.append(f"missing required key: {k}")
    if "exe" in profile and not os.path.isfile(profile["exe"]):
        issues.append(f"exe not found: {profile['exe']}")
    if "environment_mode" in profile and profile["environment_mode"] not in ("clean", "uevr", "inherit"):
        issues.append(f"environment_mode must be one of clean/uevr/inherit")
    if "capture_trigger" in profile and profile["capture_trigger"] not in ("hotkey", "frame", "delay"):
        issues.append(f"capture_trigger must be one of hotkey/frame/delay")
    return {"profile": profile.get("name"), "ok": not issues, "issues": issues}


def _make_environment(mode: str) -> list:
    """Return a list of EnvironmentModification per RenderDoc's API.

    'clean'    : strip UEVR-related variables
    'uevr'     : leave as-is, optionally add explicit UEVR vars
    'inherit'  : pass everything through
    """
    mods = []
    if mode == "clean":
        for key in ("UEVR_PATH", "UEVR_RUNTIME", "UEVR_PROFILE", "UEVR_LOG_LEVEL"):
            mod = rd.EnvironmentModification()
            mod.name = key
            mod.value = ""
            mod.mod = rd.EnvMod.Set
            mod.sep = rd.EnvSep.NoSep
            mods.append(mod)
    elif mode == "uevr":
        # Caller is expected to source the right vars elsewhere — we don't
        # invent values, we just don't strip them.
        pass
    return mods


def run(profile: dict) -> dict:
    if not validate(profile)["ok"]:
        return {"ok": False, "validate": validate(profile)}

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out_dir = profile.get("out_dir") or os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        prefix = profile.get("output_name_prefix") or profile["name"].replace(" ", "_")
        template = os.path.join(out_dir, f"{prefix}_%Y%m%d_%H%M%S")

        opts = rd.CaptureOptions()
        opts.captureCallstacks = False
        opts.captureCallstacksOnlyActions = False
        opts.captureAllCmdLists = True
        opts.delayForDebugger = 0
        opts.refAllResources = False
        opts.hookIntoChildren = False
        opts.verifyBufferAccess = False

        env_mods = _make_environment(profile.get("environment_mode", "inherit"))

        ident = rd.ExecuteAndInject(
            profile["exe"],
            profile.get("working_dir", os.path.dirname(profile["exe"])),
            profile.get("args", ""),
            env_mods,
            template,
            opts,
            False,
        )
        if ident == 0:
            return {"ok": False, "error": "ExecuteAndInject returned 0"}

        target = rd.CreateTargetControl(rd.RemoteHost(), ident, "automation", True)
        trigger = profile.get("capture_trigger", "hotkey")
        if trigger == "frame":
            frame = int(profile.get("capture_frame", 1))
            target.TriggerCapture(frame)
        elif trigger == "delay":
            time.sleep(float(profile.get("capture_delay_seconds", 5.0)))
            target.TriggerCapture(1)
        # 'hotkey' is the default — the user presses F12 in-app.

        captures = []
        deadline = time.time() + float(profile.get("max_wait_seconds", 120))
        while time.time() < deadline:
            msg = target.ReceiveMessage(None)
            if msg.type == rd.TargetControlMessageType.NewCapture:
                captures.append(str(msg.newCapture.path))
                break
            time.sleep(0.05)

        try:
            target.Shutdown()
        except Exception:
            pass

        return {"ok": True, "ident": ident, "captures": captures}
    finally:
        rd.ShutdownReplay()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Deterministic launch profiles.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate")
    pv.add_argument("profile")

    pr = sub.add_parser("run")
    pr.add_argument("profile")

    args = p.parse_args(argv)
    with open(args.profile, "r", encoding="utf-8") as f:
        profile = json.load(f)

    if args.cmd == "validate":
        print(json.dumps(validate(profile), indent=2))
        return 0
    out = run(profile)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
