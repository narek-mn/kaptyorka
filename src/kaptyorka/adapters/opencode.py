"""OpenCode: sessions and push through the TUI's local server (started with `--port 0`)."""
import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kaptyorka.adapters.base import DeliveryError, Session, merge_json

TIMEOUT_S = 5
ALIAS = "alias opencode='opencode --port 0'"
PLUGIN = """\
// kaptyorka: adds the team brief to every OpenCode chat. Written by `kaptyorka join`.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
const KAPTYORKA = %s;
export const Kaptyorka = async ({ directory }) => {
  const brief = {};
  const hook = async (sessionID, event) => {
    try {
      const run = exec(KAPTYORKA, ["hook", "opencode", event], { cwd: directory, timeout: 4000,
        env: { ...process.env, KAPTYORKA_OPENCODE_SESSION: sessionID } });
      run.child.stdin.end();
      return (await run).stdout.trim();
    } catch {
      return "";
    }
  };
  return {
    "chat.message": async (input) => {
      brief[input.sessionID] = await hook(input.sessionID, input.sessionID in brief ? "prompt" : "start");
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (brief[input.sessionID]) output.system.push(brief[input.sessionID]);
    },
  };
};
"""


def _listen_ports(pid: int) -> List[int]:
    """TCP ports the process listens on: /proc on Linux, lsof elsewhere."""
    try:
        if sys.platform.startswith("linux"):
            inodes = set()
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    link = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            ports = set()
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(table) as f:
                    for line in f.readlines()[1:]:
                        c = line.split()
                        if c[3] == "0A" and c[9] in inodes:  # 0A = LISTEN
                            ports.add(int(c[1].rsplit(":", 1)[1], 16))
            return sorted(ports)
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-P", "-n", "-Fn"],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        return sorted({int(line.rsplit(":", 1)[1]) for line in r.stdout.splitlines()
                       if line.startswith("n")})
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return []


def _opencode_pids() -> List[int]:
    if sys.platform.startswith("linux"):
        pids = []
        for d in os.listdir("/proc"):
            try:
                if d.isdigit() and Path(f"/proc/{d}/comm").read_text().strip() == "opencode":
                    pids.append(int(d))
            except OSError:
                continue
        return pids
    try:
        r = subprocess.run(["pgrep", "-x", "opencode"], capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in r.stdout.split()]


def find_ports() -> Dict[int, int]:
    """pid -> server port of every running OpenCode TUI that listens (started with --port 0)."""
    out = {}
    for pid in _opencode_pids():
        ports = _listen_ports(pid)
        if ports:
            out[pid] = ports[0]
    return out


def _auth(pid: int) -> Dict[str, str]:
    """Basic auth if the TUI was started with OPENCODE_SERVER_PASSWORD (Linux only)."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = dict(v.split("=", 1) for v in raw.decode(errors="replace").split("\0") if "=" in v)
    if not env.get("OPENCODE_SERVER_PASSWORD"):
        return {}
    user = env.get("OPENCODE_SERVER_USERNAME") or "opencode"
    token = base64.b64encode(f"{user}:{env['OPENCODE_SERVER_PASSWORD']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _call(pid: int, port: int, method: str, path: str, body: Optional[dict] = None,
          timeout: float = TIMEOUT_S):
    """(status, decoded JSON or None); raises OSError (URLError included) if unreachable."""
    headers = _auth(pid)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None


def _started_ms(pid: int) -> int:
    """When the process started, in ms since the epoch (rounded down to the second)."""
    if sys.platform.startswith("linux"):
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])  # field 22
        with open("/proc/stat") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        return (btime + ticks // os.sysconf("SC_CLK_TCK")) * 1000
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                       timeout=TIMEOUT_S, env={**os.environ, "LC_ALL": "C"})
    return int(time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y"))) * 1000


class NeedsPassword(Exception):
    """The TUI's server answered 401 and its password could not be read."""


def _list(pid: int, port: int, timeout: float = TIMEOUT_S) -> list:
    status, data = _call(pid, port, "GET", "/session", timeout=timeout)
    if status == 401:
        raise NeedsPassword(f"opencode server on port {port} needs a password "
                            "(not readable on this OS)")
    return data if status == 200 and isinstance(data, list) else []


def _newest(pid: int, port: int, cwd: str) -> Optional[str]:
    """Most recently updated session in cwd since the TUI started; None before its first prompt.

    Sessions from earlier runs in the same folder are stored too; nobody sees those.
    """
    since = _started_ms(pid)
    here = [s for s in _list(pid, port) if s.get("directory") == cwd
            and s.get("time", {}).get("updated", 0) >= since]
    if not here:
        return None
    return "opencode:" + max(here, key=lambda s: s.get("time", {}).get("updated", 0))["id"]


def session_key(pid: int, cwd: str) -> Optional[str]:
    """identity.RESOLVERS["opencode"]: the key of the TUI's current session, or None for now."""
    ports = _listen_ports(pid)
    if not ports:
        return None
    try:
        return _newest(pid, ports[0], cwd)
    except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError,
            http.client.HTTPException, subprocess.SubprocessError, NeedsPassword):
        return None


class OpenCodeAdapter:
    tool = "opencode"
    prefix = "opencode"
    restart_note = "OpenCode: open a new terminal and restart OpenCode to pick up kaptyorka"

    def __init__(self, home: Optional[Path] = None,
                 find_ports: Callable[[], Dict[int, int]] = find_ports,
                 log: Callable[[str], None] = print):
        self.home = home or Path.home()
        self.find_ports = find_ports
        self.log = log
        self._locked: set = set()  # ports already logged as needing a password

    def _skip(self, e: NeedsPassword, port: int) -> None:
        if port not in self._locked:
            self._locked.add(port)
            self.log(str(e))

    def installed(self) -> bool:
        return shutil.which("opencode") is not None

    def sessions(self) -> List[Session]:
        from kaptyorka.identity import proc_cwd

        out = []
        for pid, port in self.find_ports().items():
            try:
                cwd = proc_cwd(pid)
                key = _newest(pid, port, cwd)
            except NeedsPassword as e:
                self._skip(e, port)
                continue
            except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError,
                    http.client.HTTPException, subprocess.SubprocessError):
                continue  # TUI just exited
            if key:
                out.append(Session(key, self.tool, os.path.basename(cwd.rstrip("/")), ""))
        return out

    def deliver(self, key: str, text: str) -> None:
        sid = key.partition(":")[2]
        if not sid:
            raise DeliveryError(f"not an opencode session key: {key}")
        deadline = time.monotonic() + TIMEOUT_S
        for pid, port in self.find_ports().items():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                if not any(s.get("id") == sid for s in _list(pid, port, left)):
                    continue
                status, _ = _call(pid, port, "POST", f"/session/{sid}/prompt_async",
                                  {"parts": [{"type": "text", "text": text}]},
                                  max(deadline - time.monotonic(), 0.1))
            except NeedsPassword as e:
                self._skip(e, port)
                continue
            except (OSError, ValueError, AttributeError, http.client.HTTPException) as e:
                raise DeliveryError(f"opencode session {key}: {e}") from e
            if not 200 <= status < 300:
                raise DeliveryError(f"opencode session {key} refused: HTTP {status}")
            return
        raise DeliveryError(f"opencode session {key}: no running OpenCode server has it")

    def configure(self, kaptyorka: str) -> List[str]:
        lines = []
        conf = self.home / ".config" / "opencode"
        entry = {"type": "local", "command": [kaptyorka, "mcp"], "enabled": True}

        def add_mcp(data: dict) -> bool:
            servers = data.setdefault("mcp", {})
            if servers.get("kaptyorka") == entry:
                return False
            servers["kaptyorka"] = entry
            return True

        result = merge_json(conf / "opencode.json", add_mcp)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {conf / 'opencode.json'}")
        elif result == "changed":
            lines.append(f"OpenCode: kaptyorka MCP server added to {conf / 'opencode.json'}")

        plugin = conf / "plugins" / "kaptyorka.js"
        text = PLUGIN % json.dumps(kaptyorka)
        if not plugin.exists() or plugin.read_text() != text:
            plugin.parent.mkdir(parents=True, exist_ok=True)
            plugin.write_text(text)
            lines.append(f"OpenCode: kaptyorka plugin written to {plugin}")

        shell = os.path.basename(os.environ.get("SHELL", ""))
        bash_rc = ".bash_profile" if sys.platform == "darwin" else ".bashrc"  # macOS: login shells
        rc = {"bash": bash_rc, "zsh": ".zshrc"}.get(shell)
        if rc is None:
            self.log(f"OpenCode: add `{ALIAS}` to your shell startup file so kaptyorka can push")
        else:
            rc_path = self.home / rc
            try:
                current = rc_path.read_text()
            except FileNotFoundError:
                current = ""
            if ALIAS not in current.splitlines():
                with open(rc_path, "a") as f:
                    f.write(("" if current.endswith("\n") or not current else "\n") + ALIAS + "\n")
                lines.append(f"OpenCode: `{ALIAS}` added to {rc_path}, so kaptyorka can push "
                             "into new OpenCode windows")
        return lines
