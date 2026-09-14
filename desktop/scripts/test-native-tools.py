"""Exercise the native runtime installer over its file/network CLI boundary."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

SCRIPT = Path(__file__).with_name("install-native-tools.py")
BINARY = (
    b'#!/bin/sh\nif [ "$OFFICECLI_SKIP_UPDATE" = 1 ]; then echo 1.0.144; else echo 1.0.149; fi\n'
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(BINARY)

    def log_message(self, *args):
        pass


class NativeToolsTests(unittest.TestCase):
    def test_verified_offline_tool_and_corrupt_download(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                asset = {
                    "url": f"http://127.0.0.1:{server.server_port}/officecli",
                    "size": len(BINARY),
                    "sha256": "0" * 64,
                }
                manifest = {
                    "schema": 1,
                    "officecli": {"version": "1.0.144", "targets": {"linux-x86_64": asset}},
                }
                path = root / "tools.json"
                path.write_text(json.dumps(manifest))
                args = [
                    sys.executable,
                    str(SCRIPT),
                    "--manifest",
                    str(path),
                    "--target",
                    "linux-x86_64",
                    "--runtime",
                    str(root / "runtime"),
                    "--executable",
                    "python/bin/python3.11",
                ]
                bad = subprocess.run(args, capture_output=True, text=True)
                self.assertNotEqual(bad.returncode, 0)
                self.assertIn("SHA-256", bad.stderr)
                self.assertFalse((root / "runtime/python/bin/officecli").exists())
                asset["sha256"] = hashlib.sha256(BINARY).hexdigest()
                path.write_text(json.dumps(manifest))
                good = subprocess.run(args, capture_output=True, text=True)
                self.assertEqual(good.returncode, 0, good.stderr)
                binary = root / "runtime/python/bin/officecli"
                self.assertEqual(binary.read_bytes(), BINARY)
                self.assertEqual(
                    subprocess.check_output(
                        [str(binary), "--version"],
                        text=True,
                        env={**os.environ, "OFFICECLI_SKIP_UPDATE": "1"},
                    ).strip(),
                    "1.0.144",
                )
                # Relocating the completed runtime needs neither network nor a system install.
                runtime = root / "runtime"
                (runtime / "runtime-layout.json").write_text(json.dumps({"schema": 1}))
                shutil.copy2(SCRIPT.with_name("runtime-smoke.py"), runtime / "runtime-smoke.py")
                archive = root / "runtime-core.tar.gz"
                packed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT.with_name("create-runtime-archive.py")),
                        "--source",
                        str(runtime),
                        "--output",
                        str(archive),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(packed.returncode, 0, packed.stderr)
                moved = root / "relocated"
                with tarfile.open(archive) as bundle:
                    self.assertIn("python/bin/officecli", bundle.getnames())
                    bundle.extractall(moved, filter="data")
                smoke = subprocess.run(
                    [sys.executable, str(moved / "runtime-smoke.py"), "--native-only"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(smoke.returncode, 0, smoke.stderr)
                self.assertEqual(
                    subprocess.check_output(
                        [str(moved / "python/bin/officecli"), "--version"],
                        text=True,
                        env={**os.environ, "OFFICECLI_SKIP_UPDATE": "1"},
                    ).strip(),
                    "1.0.144",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
