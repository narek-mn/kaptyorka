"""Per-machine bridge: reports running sessions and pushes messages through each tool's door."""
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from kaptyorka import __version__, facts, http, identity, offhub, optin, orders, overnight, paths, repos, skills_sync, usage
from kaptyorka.adapters.base import LOGIN_COMMANDS, TOOL_NAMES, Adapter, write_atomic
from kaptyorka.launch import LaunchError, Launcher

HEARTBEAT_S = 10
WAIT_S = 25
RETRY_S = 5
UPDATE_RETRY_S = 3600  # after a failed auto-update
UPDATE_TIMEOUT_S = 600  # a hung pipx must not block the poll loop
FACTS_S = 60
SHOW_UP_S = 120  # a started worker's session must appear in the session list within this
SEEN_KEEP = 200  # order ids remembered against replay
NO_SECRET = "this machine has no signing secret; re-join with an invite"
COMPLETE = {"claude-code"}  # the only tool whose session list is complete; the rest may miss live ones


def _said(status, body) -> str:
    """The hub's own sentence on a refused token (the shared token switched off), else ""."""
    if status == 401 and isinstance(body, str) and body.strip() not in ("", "unauthorized"):
        return ": " + body.strip()
    return ""


def log(line: str) -> None:
    """Append to ~/.kaptyorka/bridge.log and print to stderr."""
    with open(paths.home() / "bridge.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    print(line, file=sys.stderr)


def renamed(machine: str) -> Optional[str]:
    """This computer's automatic name now (account@computer, as join picks it), when `machine`
    is that account's automatic name from an older host name; None otherwise: a name someone
    chose (no account@ at its start) stays."""
    f = identity.facts()
    user, host = f.get("os_user"), f.get("host")
    if not user or not host or machine == f"{user}@{host}":
        return None
    return f"{user}@{host}" if re.fullmatch(re.escape(user) + r"@[^@\s]+", machine or "") else None


def joined_adapters(cfg: dict, adapters: List[Adapter]) -> List[Adapter]:
    """The adapters for the tools join --only named; without --only, all of them."""
    tools = cfg.get("tools")
    return adapters if tools is None else [a for a in adapters if a.tool in tools]


def install_commands(adapters: List[Adapter], exe: str, log: Callable[[str], None]) -> None:
    """At bridge start: what a newer kaptyorka adds to the tools (each tool's /hub and its hub
    indicator), so a machine that updated gets them without joining again."""
    for a in adapters:
        install = getattr(a, "install_hub", None)
        if install is not None:
            try:
                for line in install(exe):
                    log(line)
            except Exception as e:  # never stop the bridge over it
                log(f"{a.tool}: could not write its commands: {e}")


class Bridge:
    def __init__(self, cfg: dict, adapters: List[Adapter], log: Callable[[str], None], run=None,
                 launcher: Optional[Launcher] = None):
        self.cfg, self.adapters, self.log = cfg, adapters, log
        self.runner = run or subprocess.run
        self.launcher = launcher or Launcher(cfg, Path.home(), run=self.runner, log=log)
        self.seen_ids: Dict[int, dict] = self._load_seen()
        self.pending: Dict[str, tuple] = {}  # worker -> (order, launch result, monotonic start)
        self._sessions: list = []  # the session objects of the last heartbeat
        self.stop = threading.Event()
        self._problems: Dict[str, Optional[str]] = {}
        self.pending_confirm = False  # a new token is saved but the hub has not confirmed it
        self.hub: dict = {}  # the hub's last JSON answer to a heartbeat; {} before one or on 204
        # the team projects and worker keys the hub last named; kept when a beat fails, so
        # one lost answer never hides sessions. None: no list (a hub older than 0.9.11)
        self._list = offhub.load_team()
        self._mine = offhub.load_mine()  # the team projects this machine's person has a role in
        self._repos = offhub.load_repos()  # {fingerprint: project} of the registered repos
        self._facts: dict = {}
        self._facts_at: Optional[float] = None  # monotonic time of the last build; None forces one
        self._facts_thread: Optional[threading.Thread] = None  # the once-a-minute rebuild
        self._usage = usage.Sampler()
        self._gpu_queue_ask: Optional[dict] = None  # the hub's last gpu_queue ask
        self._skills_version: Optional[str] = None  # the hub's last skills_version; None until one comes
        self._updated_to = ""  # the hub version an update was last run for
        self._update_retry_at = 0.0

    def _report(self, area: str, problem: Optional[str]) -> None:
        """Log a problem once per area until that area succeeds (problem None) again."""
        if problem is not None and problem != self._problems.get(area):
            self.log(problem)
        self._problems[area] = problem

    def _machine_facts(self, sessions: list) -> dict:
        """Machine facts and git state per session folder, rebuilt at most once a minute. The
        once-a-minute rebuild runs beside the heartbeat: its tool probes can take many seconds on
        a busy machine, and the session report must not wait for them (a late report shows a
        live session offline). The first build, and one asked for (skills, GPU queue), run here."""
        if self._facts_at is None or not self._facts:
            self._build_facts(sessions)
        elif time.monotonic() - self._facts_at >= FACTS_S and not (
                self._facts_thread is not None and self._facts_thread.is_alive()):
            self._facts_at = time.monotonic()  # one rebuild at a time
            self._facts_thread = threading.Thread(target=self._build_facts, args=(list(sessions),),
                                                  name="kaptyorka-facts", daemon=True)
            self._facts_thread.start()
        return self._facts

    def _build_facts(self, sessions: list) -> None:
        repos = {}
        for cwd in {s.cwd for s in sessions if s.cwd}:
            info = facts.git(cwd, self.runner)
            if info:
                repos[cwd] = info
        current = None if self._skills_version is None else \
            skills_sync.applied_version(paths.home()) == self._skills_version
        new = {**identity.facts(), **facts.machine(self.adapters, self.runner, skills_current=current), "git": repos}
        try:  # use per OS user: numbers only; never stops the heartbeat
            new["use"] = self._usage.read()
        except Exception as e:
            self._report("use", f"reading machine use failed: {e}")
        self._checks(new)
        self._facts = new
        self._facts_at = time.monotonic()

    def _checks(self, into: Optional[dict] = None) -> None:
        """The GPU queue, when the hub asked this bridge for it; it never stops the heartbeat
        and an unreadable file is "unknown", never a log line."""
        q = self._gpu_queue_ask
        try:
            if isinstance(q, dict) and isinstance(q.get("lock"), str):
                shares = q.get("shares") if isinstance(q.get("shares"), int) else usage.GPU_SHARES
                (self._facts if into is None else into)["gpu_queue"] = {
                    **usage.read_queue(q["lock"], shares), "read_at": time.time()}
        except Exception as e:
            self._report("gpu_queue", f"reading the GPU queue failed: {e}")

    @staticmethod
    def _sendable(facts_: dict, cwds: set) -> dict:
        """The cached facts with each git read's `read_at` swapped for its `age` now, so the
        hub never compares this machine's clock with its own; git only for the folders of
        sessions reported now."""
        git = {}
        for cwd, info in facts_.get("git", {}).items():
            if cwd not in cwds:
                continue
            info = dict(info)
            read_at = info.pop("read_at", None)
            if read_at is not None:
                info["age"] = round(max(0.0, time.time() - read_at), 1)
            git[cwd] = info
        out = {**facts_, "git": git}
        if isinstance(facts_.get("use"), dict):
            use = dict(facts_["use"])
            read_at = use.pop("read_at", None)
            use["age"] = round(max(0.0, time.time() - read_at), 1) if read_at is not None else 0.0
            out["use"] = use
        if isinstance(facts_.get("gpu_queue"), dict):
            q = dict(facts_["gpu_queue"])
            read_at = q.pop("read_at", None)
            q["age"] = round(max(0.0, time.time() - read_at), 1) if read_at is not None else 0.0
            out["gpu_queue"] = q
        return out

    def _team(self):
        """(team project names or None when unknown, worker keys to report anyway)."""
        return self._list

    def _learn_team(self, status, answer) -> None:
        """Take the list from a 200 answer; one without `team` is an older hub: no list.
        Any other answer keeps the last list."""
        if status != 200 or not isinstance(answer, dict):
            return
        team, keep, mine = answer.get("team"), answer.get("keep"), answer.get("projects")
        got = answer.get("repos")
        found = {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(got, dict) else {}
        new = ({t for t in team if isinstance(t, str)} if isinstance(team, list) else None,
               {k for k in keep if isinstance(k, str)} if isinstance(keep, list) else set())
        # the help a refused call gets names only registered projects of this person
        mine = sorted(m for m in mine if isinstance(m, str) and new[0] is not None
                      and m in new[0]) if isinstance(mine, list) else None
        shared = answer.get("shared") is True  # a team machine: every session goes on the hub
        if new != self._list or mine != self._mine or found != self._repos \
                or shared != offhub.shared_machine() or not (paths.home() / offhub.TEAM).exists():
            self._list, self._mine, self._repos = new, mine, found
            offhub.save_team(*new, mine, found, shared)  # for this machine's shims and hooks

    def heartbeat(self, again: bool = True) -> None:
        offhub.clean()
        # read before the sessions: one switched off in between is reported this once and
        # dropped on the next heartbeat, never left showing as offline
        gone = offhub.gone()
        team, keep = self._team()

        def personal(s) -> bool:
            """Outside the team projects: never reported, like a session switched off. With
            no list from the hub yet, only the manager and the hub's workers are reported.
            A session that joined the team (/hub team) is reported as it chose."""
            return not (paths.in_manager_folder(s.cwd) or s.key in keep
                        or (s.key in joined and s.project == paths.COORDINATOR)
                        or (team is not None and paths.team_name(s.project) in team))
        joined = dict(optin.joined())

        def as_joined(s):
            """A joined session with its chosen project and nothing of its folder: no path, so
            no git facts or folder list for it either. Any other session's project comes from
            its folder's name or, when that is no team project, its git remote."""
            if s.key not in joined:
                project = repos.resolve(s.project, s.cwd, team, self._repos) if s.cwd else s.project
                if not optin.auto(s.key, project, s.cwd):
                    return s if project == s.project else replace(s, project=project)
                joined[s.key] = paths.COORDINATOR  # a team machine: every session is on the hub
            coordinator = joined[s.key] == paths.COORDINATOR
            return replace(s, project=joined[s.key], cwd="",
                           label=s.label or (optin.LABEL if coordinator else ""))
        sessions, tools, objs, shown, held = [], [], [], [], False
        for a in self.adapters:
            try:
                got = [as_joined(s) for s in a.sessions()]
                off = set(getattr(a, "off_keys", set()))  # kept off the hub: never reported
                off |= {s.key for s in got if offhub.kept_off(s.key)}  # the other tools' /hub off
                mine = {s.key for s in got if personal(s)}
                held = held or bool(mine - off)
                off |= mine
                found = [asdict(s) for s in got if s.key not in off]
                problem = None
            except Exception as e:  # one broken adapter must not hide the others
                found, problem = [], f"{a.tool} sessions failed: {e}"
            self._report("sessions:" + a.prefix, problem)
            sessions += found
            objs += got if problem is None else []
            shown += [s for s in got if s.key not in off] if problem is None else []
            if problem is None and a.tool in COMPLETE and team is not None:
                tools.append(a.tool)  # complete for it; with no list some are held back
        projects = self.hub.get("projects", [])
        folders = facts.folders(projects if isinstance(projects, list) else [], shown, Path.home())
        body = {"machine": self.cfg["machine"], "sessions": sessions, "tools": tools,
                "facts": {**self._sendable(self._machine_facts(shown), {s.cwd for s in shown}),
                          **({"update_failed": paths.update_reason(self._problems["update"])}
                             if self._problems.get("update") else {})},
                "folders": {p: f for p, f in folders.items()
                            if team is not None and paths.team_name(p) in team},
                # the reported sessions in overnight mode: the hub never closes them by itself
                "overnight": [s["key"] for s in sessions if self._overnight(s["key"])]}
        if gone:
            body["gone"] = gone
        status, answer = http.request("POST", self.cfg["hub"] + "/bridge/heartbeat",
                                      self.cfg["token"], body)
        if gone and status in (200, 204):
            offhub.told(gone)
        self.hub = answer if status == 200 and isinstance(answer, dict) else {}
        self._learn_team(status, answer)
        v = self.hub.get("skills_version")
        if isinstance(v, str) and v != self._skills_version:
            self._skills_version = v
            self._facts_at = None  # the next facts show the skills after this sync
        ask = self.hub.get("gpu_queue")
        if self.hub and ask != self._gpu_queue_ask:  # a lost answer keeps the last ask
            self._gpu_queue_ask = ask
            self._facts_at = None  # switched on, changed or off: read it next beat
        self._sessions = objs
        self._refresh_name()
        self._report("heartbeat", None if status in (200, 204)
                     else f"hub answered {status} to heartbeat" + _said(status, answer))
        self._sync_skills()
        self._check_pending(objs)
        if again and team is None and held and self._team()[0] is not None:
            self.heartbeat(again=False)  # the list came just now: report its sessions at once

    def _refresh_name(self) -> None:
        """Follow a new host name: the machine name join saved (account@computer) would keep
        naming every session after the old one. Only for this machine's own token: a shared
        0.1 token is mapped to its person by that name. The hub says which (an older hub does
        not: then only a config with a signing secret, which only own tokens have). The hub
        renames this device's sessions on the next heartbeat; Cursor's keys name the machine,
        so their /hub choices move along."""
        legacy = self.hub.get("legacy")
        if legacy is True or (not isinstance(legacy, bool) and not self.cfg.get("secret")):
            return
        old = self.cfg["machine"]
        new = renamed(old)
        if new is None:
            return
        offhub.rename_machine(old, new)
        self.cfg["machine"] = new  # the launcher shares this dict
        paths.save_config(self.cfg)
        self.log(f"this computer's name changed: machine {old} is now {new}")

    def _sync_skills(self) -> None:
        """Fetch the team skills when the hub's version differs from the one written here."""
        v = self.hub.get("skills_version")
        if not isinstance(v, str) or v == skills_sync.applied_version(paths.home()):
            return
        try:
            status, answer = http.request("GET", self.cfg["hub"] + "/bridge/skills", self.cfg["token"])
            if status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("skills"), list):
                raise ValueError(f"hub answered {status} to skills")
            for line in skills_sync.sync(Path.home(), str(answer.get("version", v)),
                                         answer["skills"], self.log):
                self.log(line)
            problem = None
        except Exception as e:  # skills must never stop the heartbeat
            problem = f"skills sync failed: {e}"
        self._report("skills", problem)

    # launch and close orders

    def _load_seen(self) -> Dict[int, dict]:
        """{order id: {"ok", "error"}} of the orders this bridge ran; ok None while running."""
        try:
            data = json.loads((paths.home() / "launched.json").read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in list(data.items())[-SEEN_KEEP:]:
            if k.isdigit() and isinstance(v, dict):
                out[int(k)] = {"ok": v.get("ok"), "error": str(v.get("error") or "")}
        return out

    def _remember(self, order_id: int, ok: Optional[bool], error: str = "") -> None:
        self.seen_ids.pop(order_id, None)
        self.seen_ids[order_id] = {"ok": ok, "error": error}
        while len(self.seen_ids) > SEEN_KEEP:
            del self.seen_ids[next(iter(self.seen_ids))]
        try:
            write_atomic(paths.home() / "launched.json",
                         json.dumps({str(k): v for k, v in self.seen_ids.items()}))
        except OSError as e:
            self.log(f"could not save launched.json: {e}")

    def _order(self, m: dict) -> None:
        """Verify, run and ack one launch or close order. Every outcome is acked once; an
        order run before is acked again with its stored outcome (the first ack may be lost)."""
        order = {k: v for k, v in m.items() if k != "launch"}
        oid = order.get("id")
        name = order.get("worker")
        ran, left = False, None
        try:
            secret = self.cfg.get("secret")
            if not secret:
                raise LaunchError(NO_SECRET)
            problem = orders.verify(order, orders.signing_key(secret), time.time(),
                                    self.seen_ids)
            if problem == "order already run":
                done = self.seen_ids[oid]
                ok = done["ok"] is True
                error = done["error"] if done["ok"] is not None \
                    else "the bridge stopped while running this order"
                self._ack(oid, ok, True, "" if ok else error)
                return
            if problem:
                raise LaunchError(problem)
            kind = order.get("kind")
            if kind == "launch" and "you" not in self.hub:
                # no heartbeat answer yet (just started, or the hub just restarted): whose
                # account this is stays unknown, so leave the order unacked; the hub offers
                # it again within a minute
                self.log(f"order {oid} for worker {name} waits for the hub's heartbeat answer")
                return
            if kind == "launch":
                you = self.hub.get("you")
                if order.get("person") != you and not self.hub.get("shared"):
                    raise LaunchError(f"this account belongs to {you}")
            elif kind != "close":
                raise LaunchError(f"unknown order kind {kind!r}")
            self._remember(oid, None)  # before running: a lost ack must never start it twice
            ran = True
            if kind == "launch":
                result = self.launcher.launch(order, self._sessions)
                self.pending[name] = (order, result, time.monotonic())
                self.log(f"started worker {name} in {result['folder']}")
            else:
                if order.get("auto") and self._overnight(str(order.get("key") or "")):
                    raise LaunchError("overnight mode is on in that session")
                self.pending.pop(name, None)
                self.launcher.close(order)
                self.log(f"closed worker {name}" + (f": {order['auto']}" if order.get("auto") else ""))
                if order.get("auto"):  # the boss hears what it left
                    left = self._left(str(order.get("folder") or ""))
            ok, error = True, ""
        except LaunchError as e:
            ok, error = False, str(e)
        except Exception as e:  # a bug in one order must not stop the poll loop
            ok, error = False, f"{order.get('kind') or 'order'} failed: {e}"
        if ran:
            self._remember(oid, ok, error)
        if not ok:
            self.log(f"order {oid} for worker {name} refused: {error}")
        if isinstance(oid, int) and not isinstance(oid, bool):
            if left is not None:
                self._ack(oid, ok, True, error, left=left)
            else:
                self._ack(oid, ok, True, error)

    def _worker(self, body: dict) -> Optional[int]:
        try:
            status, _ = http.request("POST", self.cfg["hub"] + "/bridge/worker",
                                     self.cfg["token"], body)
        except http.HubUnreachable:
            return None
        return status

    def _stop(self, name: str, order: dict, why: str) -> None:
        try:
            self.launcher.close(order)
            self.log(f"worker {name} {why}; stopped its session")
        except LaunchError as e:
            self.log(f"worker {name} {why}; stopping its session failed: {e}")

    def _no_show(self, name: str, order: dict) -> str:
        """Why a started worker never showed up, as far as this machine knows."""
        tool = order.get("tool") or ""
        label = TOOL_NAMES.get(tool, tool)
        if order.get("manager") is True and self.launcher.trust_prompt(order):
            # the hub's trust rule reads "does not trust": one notice, no relaunch loop
            return (f"Claude Code does not trust {order.get('folder')}; open claude there once"
                    " and accept")
        if (self._facts.get("logins") or {}).get(tool) is False and tool in LOGIN_COMMANDS:
            return (f"{label} is not logged in on this machine; the session could not start."
                    f" Run `{LOGIN_COMMANDS[tool]}` there.")
        return (f"the session did not show up within 2 minutes; check that {label} is logged in"
                f" and trusts the folder {order.get('folder')} (open it there once);"
                f" see ~/.kaptyorka/workers/{name}.log")

    def _check_pending(self, sessions: list) -> None:
        """Tell the hub each started worker's session key once it shows up, or that it never
        did (then stop what was started, so nothing is left running unwatched)."""
        for name, (order, result, started) in list(self.pending.items()):
            # what the launch learned: the folder and, for Claude Code, the id to stop
            order = {**order, "folder": result["folder"], "key": result["key"],
                     "job": result.get("job")}
            try:
                key = self.launcher.find_key(order, sessions)
                if key:
                    body = {"worker": name, "key": key}
                    if result.get("needs_brief"):
                        body["needs_brief"] = True
                    status = self._worker(body)
                    if status == 410:  # the worker was closed or failed meanwhile
                        self.pending.pop(name, None)
                        self._stop(name, order, "was closed while starting")
                    elif status == 404:
                        self.pending.pop(name, None)
                        self.log(f"worker {name}: the hub does not know it as starting here")
                    elif status == 204:
                        self.pending.pop(name, None)
                elif time.monotonic() - started > SHOW_UP_S:
                    status = self._worker({"worker": name, "error": self._no_show(name, order)})
                    if status in (204, 404, 410):
                        self.pending.pop(name, None)
                        self._stop(name, order, "did not show up within 2 minutes")
            except Exception as e:  # one worker must not block the others or the heartbeat
                self.pending.pop(name, None)
                self.log(f"worker {name}: {e}")

    @staticmethod
    def _overnight(key: str) -> bool:
        """True while overnight mode is on in this Claude Code session (none other has it)."""
        return key.startswith("claude:") and overnight.state(key.split(":", 1)[1]) is not None

    def _left(self, folder: str) -> dict:
        """What a closed worker left in its folder: the folder, its branch and how many files
        are uncommitted (none when it is no git repository)."""
        out = {"folder": folder}
        if not folder:
            return out
        def git(*args):
            try:
                r = self.runner(["git", "-C", folder, *args], capture_output=True, text=True,
                                timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return r.stdout if r.returncode == 0 else None
        branch, status = git("rev-parse", "--abbrev-ref", "HEAD"), git("status", "--porcelain")
        if branch is not None:
            out["branch"] = branch.strip()
            if status is not None:
                out["uncommitted"] = len([x for x in status.splitlines() if x.strip()])
        return out

    def _ack(self, msg_id: int, ok: bool, launch: bool = False, error: str = "",
             left: Optional[dict] = None) -> None:
        url, body = self.cfg["hub"] + "/bridge/ack", {"id": msg_id, "ok": ok}
        if launch:
            body.update(launch=True, error=error)
            if left is not None:
                body["left"] = left
        elif error:
            body["error"] = error  # shown with the message: why it was not pushed
        try:
            status, _ = http.request("POST", url, self.cfg["token"], body)
        except http.HubUnreachable:
            status, _ = http.request("POST", url, self.cfg["token"], body)
        self._report("ack", None if status == 204 else f"hub answered {status} to ack")

    def _rekey(self, m: dict) -> None:
        """The hub gave this legacy machine a token of its own: save it, switch to it, confirm."""
        self.cfg.update(token=m["token"], secret=m["secret"])
        paths.save_config(self.cfg)
        self.pending_confirm = True
        self._confirm()

    def _confirm(self) -> None:
        """Tell the hub the new token is in use; retried on each poll until it answers 204.
        The hub also drops the pending rekey on the first call made with the new token."""
        try:
            status, _ = http.request("POST", self.cfg["hub"] + "/bridge/rekeyed",
                                     self.cfg["token"], {"machine": self.cfg["machine"]})
        except http.HubUnreachable:
            status = None
        if status == 204:
            self.pending_confirm = False
            self._report("rekey", None)
            self.log("rekeyed: this machine now has its own token")
        else:
            self._report("rekey", f"hub answered {status} to rekeyed" if status
                         else "hub unreachable when confirming the new token")

    def poll_once(self, wait: float = WAIT_S) -> int:
        if self.pending_confirm:
            self._confirm()
        query = urlencode({"machine": self.cfg["machine"], "wait": wait})
        status, items = http.request("GET", f"{self.cfg['hub']}/bridge/poll?{query}",
                                     self.cfg["token"], timeout=wait + 5)
        if status != 200:
            raise RuntimeError(f"hub answered {status} to poll" + _said(status, items))
        by_prefix = {a.prefix: a for a in self.adapters}
        delivered = 0
        for m in items:
            if m.get("kind", "message") == "rekey":
                self._rekey(m)
                continue
            if m.get("launch") is True:
                self._order(m)
                continue
            key = m["key"]
            try:
                adapter = by_prefix.get(key.split(":", 1)[0])
                if adapter is None:
                    raise LookupError("no adapter for this tool")
                if offhub.kept_off(key):
                    raise LookupError("that session is off the hub")
                if key.startswith("claude:") and overnight.state(key.split(":", 1)[1]):
                    overnight.delivered(key.split(":", 1)[1], m["text"])  # never a person
                adapter.deliver(key, m["text"])
                ok = True
            except Exception as e:  # any failure leaves the message for pull
                self.log(f"push to {key} failed: {e}; message {m['id']} left for pull")
                ok, error = False, paths.push_reason(str(e))  # the full text stays in the log
            else:
                error = ""
            self._ack(m["id"], ok, error=error)
            delivered += ok
        return delivered

    def _step(self) -> float:
        """One poll; returns how long to wait before the next. Logs each problem once."""
        try:
            delivered = self.poll_once(WAIT_S)
        except (http.HubUnreachable, RuntimeError) as e:
            problem = (f"kaptyorka hub unreachable at {self.cfg['hub']}"
                       if isinstance(e, http.HubUnreachable) else str(e))
            self._report("poll", problem)
            return RETRY_S
        self._report("poll", None)
        self._maybe_update(delivered)
        return 0

    @staticmethod
    def _numbers(version) -> tuple:
        return tuple(int(n) for n in re.findall(r"\d+", str(version or "")))

    def _maybe_update(self, delivered: int) -> None:
        """Run `kaptyorka update` once per hub version, only at a quiet moment."""
        new = self.hub.get("version")
        if (not self.hub.get("auto_update") or delivered or self.pending
                or new == self._updated_to
                or self._numbers(new) <= self._numbers(__version__)
                or time.monotonic() < self._update_retry_at):
            return
        argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
        exe = argv0 if argv0 and argv0.name == "kaptyorka" else Path(sys.executable).parent / "kaptyorka"
        self.log(f"updating kaptyorka {__version__} -> {new}")
        try:
            r = self.runner([str(exe), "update"], capture_output=True, text=True, timeout=UPDATE_TIMEOUT_S)
            error = "" if r.returncode == 0 else (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
            if not error and "already the latest" in (r.stdout or ""):
                error = "no newer release found"  # this process still runs the old version
        except Exception as e:
            error = str(e)
        if error:
            self._report("update", f"auto-update failed: {error}")
            self._update_retry_at = time.monotonic() + UPDATE_RETRY_S
        else:
            self._report("update", None)
            self._updated_to = new

    def _beat(self) -> None:
        while not self.stop.is_set():
            try:
                self.heartbeat()
            except http.HubUnreachable:
                pass  # the poll loop reports the outage
            except Exception as e:
                self._report("heartbeat", f"heartbeat failed: {e}")
            self.night_watch()
            self.stop.wait(HEARTBEAT_S)

    def night_watch(self) -> None:
        """Overnight sessions: the 16-hour cap, a crash (claude --resume in tmux, at most twice),
        a usage limit or API error ("continue", for at most 6 hours). Never stops the beat."""
        claude = next((a for a in self.adapters if a.prefix == "claude"), None)
        if claude is None:
            return

        def resume(sid: str, st: dict):
            if shutil.which("tmux") is None:
                return False, "tmux is not installed, so it was not restarted"
            try:
                r = subprocess.run(overnight.resume_argv(sid, st), capture_output=True,
                                   text=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                return False, f"restart failed ({type(e).__name__})"
            if r.returncode:
                return False, "restart failed (tmux said no)"
            return True, "restarted with claude --resume in tmux"
        try:
            overnight.watch(lambda sid: offhub.session(sid, claude.home) is not None,
                            lambda sid, text: claude.deliver(f"claude:{sid}", text),
                            resume, overnight.finish)
        except Exception as e:
            self.log(f"overnight watch failed: {e}")

    def run(self) -> None:
        self._refresh_name()  # before the first heartbeat names any session
        beat = threading.Thread(target=self._beat, daemon=True)
        beat.start()
        try:
            while not self.stop.is_set():
                self.stop.wait(self._step())
        finally:
            self.stop.set()
            beat.join()
