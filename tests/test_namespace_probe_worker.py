from __future__ import annotations

import http.server
import socketserver
import threading
import unittest

from firmarbiter_core.namespace_probe_worker import (
    capture_http,
    probe_reachability,
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"firmarbiter-probe-test"
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain",
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(
        self,
        _format: str,
        *args: object,
    ) -> None:
        del args


class NamespaceProbeWorkerTests(unittest.TestCase):
    def test_reachability_and_http_snapshot(self) -> None:
        server = socketserver.TCPServer(
            ("127.0.0.1", 0),
            _Handler,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        thread.start()

        try:
            port = int(server.server_address[1])
            endpoint = {
                "host": "127.0.0.1",
                "port": port,
                "protocol": "http",
            }

            reachability = probe_reachability(
                {
                    "endpoint": endpoint,
                    "timeout_seconds": 1.0,
                    "attempts": 1,
                }
            )
            snapshot = capture_http(
                {
                    "endpoint": endpoint,
                    "timeout_seconds": 1.0,
                }
            )

            self.assertEqual(
                reachability["status"],
                "true",
            )
            self.assertEqual(
                snapshot["status"],
                "captured",
            )
            self.assertEqual(
                snapshot["http_status"],
                200,
            )
            self.assertEqual(
                len(snapshot["body_sha256"]),
                64,
            )

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
