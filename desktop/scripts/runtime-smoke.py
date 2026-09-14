#!/usr/bin/env python3
"""Smoke-test the relocatable Python runtime embedded in desktop bundles."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

REQUIRED_MODULES = (
    "agentscope",
    "fastapi",
    "httpx",
    "mcp",
    "numpy",
    "pandas",
    "pikepdf",
    "pymilvus",
    "scipy",
    "sqlalchemy",
    "uvicorn",
)


def check_native_tools() -> dict:
    root = Path(__file__).resolve().parent
    tools = json.loads((root / "native-tools.json").read_text(encoding="utf-8"))
    tool = tools["officecli"]
    binary = root / tool["path"]
    binary.resolve().relative_to(root)
    if not binary.is_file():
        raise RuntimeError("Bundled OfficeCLI is missing")
    # Do not inherit developer tool directories or cloud credentials. The CLI
    # must run from the relocated private runtime on the end user's machine.
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "HOME", "USERPROFILE", "LANG")
    }
    env["PATH"] = str(binary.parent) + os.pathsep + os.defpath
    env["DOTNET_EnableDiagnostics"] = "0"
    env["OFFICECLI_SKIP_UPDATE"] = "1"  # the desktop release owns this binary
    result = subprocess.run(
        [str(binary), "--version"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    if not re.search(r"(?<![0-9.])" + re.escape(tool["version"]) + r"(?![0-9.])", result.stdout):
        raise RuntimeError("Bundled OfficeCLI version does not match the manifest")
    return {"officecli": tool["version"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--native-only", action="store_true")
    args = parser.parse_args()

    native_tools = check_native_tools()
    if args.native_only:
        print(json.dumps({"ok": True, "native_tools": native_tools}))
        return 0

    imported: list[str] = []
    for module_name in REQUIRED_MODULES:
        importlib.import_module(module_name)
        imported.append(module_name)

    if args.source:
        backend = args.source.resolve() / "src" / "backend"
        if not (backend / "cli.py").is_file():
            raise RuntimeError(f"CE source is missing cli.py: {backend}")
        sys.path.insert(0, str(backend))
        importlib.import_module("cli")
        imported.append("cli")

    print(
        json.dumps(
            {
                "ok": True,
                "python": platform.python_version(),
                "executable": sys.executable,
                "modules": imported,
                "native_tools": native_tools,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
