"""A fake OpenCode TUI server: GET /session and POST /session/<id>/prompt_async, recorded."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenCode:
    def __init__(self, sessions):
        self.sessions = sessions
        self.requests = []  # (method, path, body or None)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                self._send(200, json.dumps(fake.sessions).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, body))
                self._send(204, b"")

            def _send(self, status, data):
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
