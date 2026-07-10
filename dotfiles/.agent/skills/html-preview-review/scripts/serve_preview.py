#!/usr/bin/env python3
"""Serve one private HTML preview once over an ephemeral loopback port."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import time
from typing import ClassVar
from urllib.parse import urlsplit


HOST = "127.0.0.1"
REQUEST_TIMEOUT_SECONDS = 30.0


class PreviewServer(HTTPServer):
    deadline: float
    html: bytes
    served = False

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, client_address = super().get_request()
        remaining = max(0.001, self.deadline - time.monotonic())
        request.settimeout(remaining)
        return request, client_address


class PreviewHandler(BaseHTTPRequestHandler):
    server: PreviewServer
    server_version = "HTMLPreview"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    responses: ClassVar[dict[int, tuple[str, str]]] = BaseHTTPRequestHandler.responses

    def _send_headers(self, status: int, content_length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True

    def _is_preview_path(self) -> bool:
        return urlsplit(self.path).path == "/index.html"

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._is_preview_path():
            self._send_headers(404, 0)
            return
        self._send_headers(200, len(self.server.html))

    def do_GET(self) -> None:  # noqa: N802
        if not self._is_preview_path():
            self._send_headers(404, 0)
            return
        self._send_headers(200, len(self.server.html))
        self.wfile.write(self.server.html)
        self.server.served = True

    def log_message(self, format: str, *args: object) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Rendered index.html path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve(strict=True)
    if not input_path.is_file():
        raise SystemExit("input must be a regular file")

    with PreviewServer((HOST, 0), PreviewHandler) as server:
        server.html = input_path.read_bytes()
        port = server.server_address[1]
        print(f"http://{HOST}:{port}/index.html", flush=True)

        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        server.deadline = deadline
        while not server.served:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SystemExit("preview request timed out")
            server.timeout = remaining
            server.handle_request()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
