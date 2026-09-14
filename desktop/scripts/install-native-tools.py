#!/usr/bin/env python3
"""Download pinned native desktop tools at build time, never at user installation."""
import argparse
import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath


def install(manifest_path: Path, target: str, runtime: Path, executable: str) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported native tools manifest")
    tool = manifest["officecli"]
    asset = tool["targets"][target]
    # Place beside the private Python: the host runner already exposes this
    # trusted directory in its clean PATH on all desktop platforms.
    relative = PurePosixPath(executable).parent / (
        "officecli.exe" if target.startswith("windows-") else "officecli"
    )
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Native tool path must stay inside the runtime")
    destination = runtime.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".officecli-", dir=destination.parent)
    os.close(descriptor)
    try:
        for attempt in range(3):
            try:
                digest = hashlib.sha256()
                size = 0
                request = urllib.request.Request(
                    asset["url"], headers={"User-Agent": "desktop-runtime-builder"}
                )
                with (
                    urllib.request.urlopen(request, timeout=120) as response,
                    open(temporary, "wb") as output,
                ):
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > asset["size"]:
                            raise ValueError("OfficeCLI download exceeds pinned size")
                        digest.update(chunk)
                        output.write(chunk)
                break
            except (OSError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise ValueError("OfficeCLI SHA-256/size verification failed")
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "officecli": {
            "version": tool["version"],
            "path": relative.as_posix(),
            "upstream_sha256": asset["sha256"],
        }
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--executable", required=True)
    args = parser.parse_args()
    tools = install(args.manifest, args.target, args.runtime, args.executable)
    (args.runtime / "native-tools.json").write_text(
        json.dumps(tools, indent=2) + "\n", encoding="utf-8"
    )
    print("Verified and staged OfficeCLI " + tools["officecli"]["version"])


if __name__ == "__main__":
    main()
