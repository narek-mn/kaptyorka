"""A fake hub for the relay ("evilhub" of the secure design, section 12): it logs every byte it
gets and answers whatever the test tells it to. Standard library only, so the same file runs in
the fast tests and in the throw-away test container (testbox/slice1.sh).

In a test:   hub = FakeHub(cert, key); hub.answers["send"] = (200, {}, {"ok": True, "x": 1})
From a shell: python3 relay_hub.py --cert C --key K --host 10.77.0.1 --port 8443 \
                  --log /var/log/fakehub.log --answers /etc/fakehub-answers.json [--plain] [--tag T]
The answers file is read again on every request: {"<call>": {"status": 200, "headers": {},
"body": <JSON value, or a string sent as it is>}}. A call with no answer gets
{"ok": true, "echo": <the body it got>}, which the relay must drop (only "ok" may pass)."""
import argparse
import json
import socket
import ssl
import threading
from typing import Any, Dict, List, Optional, Tuple


class FakeHub:
    def __init__(self, cert: Optional[str], key: Optional[str], host: str = "127.0.0.1",
                 port: int = 0, log_path: Optional[str] = None, answers_path: Optional[str] = None,
                 tag: str = "hub"):
        self.answers: Dict[str, Tuple[int, Dict[str, str], Any]] = {}
        self.requests: List[Dict[str, Any]] = []
        self.log_path, self.answers_path, self.tag = log_path, answers_path, tag
        self.ctx = None
        if cert:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(cert, key)
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.sock = socket.socket(fam, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _log(self, raw: bytes) -> None:
        if self.log_path:
            with self.lock, open(self.log_path, "ab") as f:
                f.write(b"\n=== " + self.tag.encode() + b"\n" + raw + b"\n")

    def _answer_for(self, call: str, body: bytes) -> Tuple[int, Dict[str, str], Any]:
        if self.answers_path:
            try:
                with open(self.answers_path) as f:
                    a = json.load(f).get(call)
                if a is not None:
                    return a.get("status", 200), a.get("headers", {}), a.get("body")
            except (OSError, ValueError):
                pass
        if call in self.answers:
            return self.answers[call]
        try:
            echo = json.loads(body)
        except ValueError:
            echo = body.decode("utf-8", "replace")
        return 200, {}, {"ok": True, "echo": echo}

    def _loop(self) -> None:
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c: socket.socket) -> None:
        raw = b""
        try:
            c.settimeout(5)
            if self.ctx:
                c = self.ctx.wrap_socket(c, server_side=True)
            while b"\r\n\r\n" not in raw:
                got = c.recv(65536)
                if not got:
                    break
                raw += got
            head, _, body = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:]:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", "0") or 0)
            while len(body) < n:
                got = c.recv(65536)
                if not got:
                    break
                body += got
            raw = head + b"\r\n\r\n" + body
            self._log(raw)
            path = lines[0].split(" ")[1] if " " in lines[0] else ""
            with self.lock:
                self.requests.append({"line": lines[0], "path": path, "headers": headers,
                                      "body": body, "raw": raw})
            call = path.rsplit("/", 1)[-1]
            status, extra, answer = self._answer_for(call, body)
            data = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
            h = {"Content-Type": "application/json", **extra}
            out = f"HTTP/1.1 {status} X\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
            out += f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n"
            c.sendall(out.encode() + data)
        except Exception as e:  # a test aid: log and go on
            self._log(b"ERROR " + repr(e).encode() + b" after " + raw[:200])
        finally:
            try:
                c.close()
            except OSError:
                pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--log")
    p.add_argument("--answers")
    p.add_argument("--tag", default="hub")
    p.add_argument("--plain", action="store_true", help="plain HTTP, no TLS")
    a = p.parse_args()
    FakeHub(None if a.plain else a.cert, None if a.plain else a.key, a.host, a.port, a.log,
            a.answers, a.tag)
    threading.Event().wait()
