"""Optional disposable browser, with all page requests brokered through research HTTP policy."""
from __future__ import annotations

import base64
import json
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shams import research_http


def render(research, url: str, deadline: float) -> tuple[str, str, str]:
    origin = research_http.origin(research_http.parts(url))
    deadline = min(deadline, time.monotonic() + research.timeout)
    cfg = research.browser
    nonce = "/" + secrets.token_urlsafe(24)
    lock = threading.Lock()
    request_count = 0
    bytes_left = 4_000_000

    class Broker(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal request_count, bytes_left
            if self.path != nonce:
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("invalid request length")
                target = json.loads(self.rfile.read(size))["url"]
                with lock:
                    request_count += 1
                    if request_count > 24 or bytes_left <= 0:
                        raise ValueError("browser resource budget reached")
                    data, mime, final, status = research._get(target, deadline=deadline, public_origin=origin)
                    if len(data) > bytes_left:
                        raise ValueError("browser byte budget reached")
                    bytes_left -= len(data)
                result = {"body": base64.b64encode(data).decode("ascii"), "mime": mime, "url": final, "status": status}
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, OSError, KeyError, TypeError):
                self.send_error(403)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Broker) as server:
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        try:
            command = [str(cfg.get("node") or "node"), str(Path(__file__).parent / "js" / "research_browser.cjs")]
            payload = {"url": url, "broker": f"http://127.0.0.1:{server.server_port}{nonce}",
                       "module": cfg.get("module") or "playwright", "executable": cfg.get("executable"),
                       "timeout": int(research_http.remaining(deadline) * 1000)}
            result = subprocess.run(command, input=json.dumps(payload), capture_output=True, encoding="utf-8",
                                    timeout=research_http.remaining(deadline) + 1,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            if result.returncode:
                raise ValueError(f"browser unavailable: {result.stderr.strip()[:250]}")
            page = json.loads(result.stdout)
            if not page["text"].strip():
                raise ValueError("browser did not find readable text")
            return page["url"], page["title"][:180], page["text"][:4800]
        finally:
            server.shutdown()
