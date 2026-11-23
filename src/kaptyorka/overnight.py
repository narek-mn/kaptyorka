"""Overnight tasks, Claude Code only: a session left alone for the night keeps working inside the
night rules, and a morning report says what happened. docs/superpowers/specs/2026-11-20-overnight-tasks.md

Everything is kept on this machine under paths.home()/overnight, readable by its user only:
<session id>.json (the state), <session id>.log (what happened, one JSON line each, secrets
dropped), <session id>.sent (heads of the texts kaptyorka delivered to it, so a hub message never
counts as a person). The developer's lists are paths.home()/overnight.json.

Hook decisions used (code.claude.com/docs/en/hooks, read 2026-11-20, Claude Code 2.1.329):
PreToolUse `permissionDecision: "deny"` with a reason Claude reads (never "allow": deny and ask
rules then stay the developer's, docs/en/permissions "Precedence among rules, hooks, and mods");
PermissionRequest `decision.behavior` allow/deny with `message` (fires only when a person would be
asked; it cannot approve what a deny rule refuses); Stop `decision: "block"` + `reason`;
StopFailure `error`; UserPromptSubmit `additionalContext` and `systemMessage`."""
import fnmatch
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from kaptyorka import paths
from kaptyorka.secrets_check import PATTERNS

CAP_S = 16 * 3600  # the safety cap; nothing else is time-based
NUDGES = 3  # "continue" after a stop, in a row
RESUMES = 2  # restarts after a crash, per night
WAIT_S = 6 * 3600  # how long a usage limit or an API error is waited out
PUSH_GAP_S = {"rate_limit": 1800}  # between "continue" pushes while waiting; other errors:
PUSH_GAP_OTHER_S = 300
RESUME_GRACE_S = 120  # after a restart, before the session counts as gone again
SENT_KEEP_S = 86400
DONE_MARK = "OVERNIGHT DONE"
SHARED = ["main", "develop"]  # a project's shared branches unless the developer names others
CLASSES = ("admin", "git", "delete", "hardware")
LABEL = {
    "admin": "sudo, su, pkexec, doas",
    "git": "force push or history rewrite on shared branches",
    "delete": "deleting system folders, the home folder, other users' folders or protected folders",
    "hardware": "commands that move real hardware",
    "unclear": "a command the night rules cannot read",
    "rules": "changing the overnight rules",
}
ADMIN = {"sudo", "su", "pkexec", "doas"}
SYSTEM = ("/etc", "/usr", "/boot", "/var", "/opt", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
          "/srv", "/root", "/sys", "/proc", "/dev", "/snap", "/System", "/Library",
          "/Applications", "/private", "/cores", "/Volumes")
NOT_SYSTEM = ("/var/tmp", "/private/tmp", "/private/var/folders", "/var/folders", "/dev/shm")
HOMES = ("/home", "/Users")
WRAPPERS = {"env", "nice", "nohup", "time", "command", "builtin", "exec", "timeout", "stdbuf",
            "ionice", "setsid", "chronic", "unbuffer", "caffeinate", "flock"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
DELETERS = {"rm", "rmdir", "unlink", "shred", "srm", "trash", "trash-put", "gio"}
WRITERS = {"cp", "install", "tee", "truncate", "ln", "touch", "mkdir", "dd", "rsync", "mv", "sed",
           "perl", "chmod", "chown", "chgrp"}
REDIRS = {">", ">>", "&>", "&>>", ">&", ">|", "<", "<<", "<<<", "<<-", "<&", ">>&"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

TRIGGER = re.compile(r"\bover\s?-?night\b|\bwhile i(?:'m| am)?\s+(?:sleep|sleeping|asleep|away)\b"
                     r"|\bbefore (?:the )?morning\b|\bby (?:the )?morning\b", re.I)
HUB_OVERNIGHT = re.compile(r"\s*/hub\s+overnight(?:\s+(on|off))?\s*", re.I)
YES = re.compile(r"\s*(ok|okay|yes|yep|go|confirm(?:ed)?|fine|sounds good|good)[.!]*\s*", re.I)
ALLOW = [  # "you may move the motors": one class, for this task
    ("hardware", re.compile(r"\b(motors?|hardware|robot|device|actuators?|arm|servos?)\b", re.I)),
    ("admin", re.compile(r"\b(sudo|su|pkexec|doas|admin|root)\b", re.I)),
    ("git", re.compile(r"\b(force[- ]push|push --force|rewrite (?:the )?history|rebase main)\b", re.I)),
    ("delete", re.compile(r"\b(delete|remove|rm)\b.*\b(system|protected|recordings?|folders?)\b", re.I)),
]
MAY = re.compile(r"\b(?:you may|you can|allowed to|i allow|allow you to|go ahead and)\b(.*)", re.I)
LIST = re.compile(r"\b(hardware(?: list)?|protect(?:ed)?(?: folders?)?|shared branch(?:es)?)\s*:\s*(.+)",
                  re.I)


# ---------- files ----------

def _dir() -> Path:
    d = paths.home() / "overnight"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _private(path: Path, text: str) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _append(path: Path, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def _sid(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(sid))[:80]


def state(sid: str) -> Optional[dict]:
    """The session's overnight state while it is on; None when off or unknown."""
    f = paths.home() / "overnight" / f"{_sid(sid)}.json"
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and st.get("on") else None


def save(sid: str, st: dict) -> None:
    _private(_dir() / f"{_sid(sid)}.json", json.dumps(st))


def active() -> List[Tuple[str, dict]]:
    d = paths.home() / "overnight"
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        st = state(f.stem)
        if st is not None:
            out.append((f.stem, st))
    return out


def safe(text: str, cap: int = 300) -> str:
    """One line, capped, or "" when it looks like a secret (the knowledge digest's checker)."""
    line = " ".join(str(text).split())[:cap]
    return "" if any(p.search(line) for _, p in PATTERNS) else line


def note(sid: str, kind: str, text: str, now: Optional[float] = None) -> None:
    """kind: done, decision, skipped, clash, event."""
    line = safe(text)
    if line:
        _append(_dir() / f"{_sid(sid)}.log",
                json.dumps({"t": time.time() if now is None else now, "kind": kind, "text": line}))


def notes(sid: str) -> List[dict]:
    try:
        lines = (paths.home() / "overnight" / f"{_sid(sid)}.log").read_text().splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def config() -> dict:
    try:
        c = json.loads((paths.home() / "overnight.json").read_text())
    except (OSError, ValueError):
        c = {}
    return c if isinstance(c, dict) else {}


def save_config(c: dict) -> None:
    paths.home().mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(paths.home() / "overnight.json", json.dumps(c, indent=2) + "\n")


def lists(project: str) -> dict:
    """{"hardware", "protected", "shared"} for a project: the machine's lists plus the project's."""
    c = config()
    m = c.get("machine") if isinstance(c.get("machine"), dict) else {}
    p = (c.get("projects") or {}).get(project) if isinstance(c.get("projects"), dict) else None
    p = p if isinstance(p, dict) else {}

    def get(d, k):
        v = d.get(k)
        return [str(x) for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []
    shared = get(p, "shared") or get(m, "shared") or list(SHARED)
    return {"hardware": get(m, "hardware") + get(p, "hardware"),
            "protected": get(m, "protected") + get(p, "protected"), "shared": shared}


# ---------- the command classifier ----------

class Unclear(Exception):
    pass


def _norm(path: str, cwd: str, home: str) -> str:
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    elif path.startswith("~"):  # ~bob: another user's home
        path = "/home/" + path[1:] if not path[1:].startswith("/") else path
    for v in ("$HOME", "${HOME}"):
        if path == v or path.startswith(v + "/"):
            path = home + path[len(v):]
    if "$" in path or "`" in path:
        raise Unclear(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _glob_base(path: str) -> str:
    """A path with * ? [ in it: the folder before the first such part (deleting /usr/* is
    deleting /usr)."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        if any(c in part for c in "*?["):
            return "/".join(parts[:i]) or "/"
    return path


def _inside(path: str, folder: str) -> bool:
    folder = folder.rstrip("/") or "/"
    return path == folder or path.startswith(folder + "/") or folder == "/"


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _own(path: str, ctx: dict) -> bool:
    return any(_inside(path, o) or _inside(_real(path), o) or _inside(o, path)
               for o in ctx.get("own", []))


def _protected(ctx: dict) -> List[str]:
    out = []
    for p in ctx["lists"]["protected"]:
        try:
            n = _norm(p, ctx["cwd"], ctx["home"])
        except Unclear:
            continue
        out += [n, _real(n)]
    return out


def delete_blocked(path: str, ctx: dict) -> str:
    """Why deleting this resolved path is blocked ("" when allowed); symlinks are followed."""
    home = ctx["home"]
    for p in {path, _real(path)}:
        if p == "/" or p in HOMES or p == home.rstrip("/"):
            return f"{p} is a system folder or the home folder itself"
        if _inside(home, p):  # a parent of the home folder
            return f"{p} holds the home folder"
        if not any(_inside(p, n) for n in NOT_SYSTEM) and any(_inside(p, s) for s in SYSTEM):
            return f"{p} is in a system folder"
        for h in HOMES:
            if _inside(p, h) and not _inside(p, home) and p != h:
                return f"{p} is in another user's home folder"
        for q in _protected(ctx):
            if _inside(p, q) or _inside(q, p):
                return f"{p} touches the protected folder {q}"
    return ""


def write_blocked(path: str, ctx: dict) -> str:
    """Overwriting or creating at this path: only the protected folders are blocked."""
    for p in {path, _real(path)}:
        for q in _protected(ctx):
            if _inside(p, q):
                return f"{p} is in the protected folder {q}"
    return ""


def _hardware_hit(words: List[str], raw: str, ctx: dict) -> str:
    for entry in ctx["lists"]["hardware"]:
        e = entry.strip()
        if "/" in e:
            try:
                target = _norm(e, ctx["cwd"], ctx["home"])
            except Unclear:
                continue
            if e in raw or target in raw:
                return e
            for w in words:
                try:
                    n = _norm(w, ctx["cwd"], ctx["home"])
                except Unclear:
                    continue
                if _inside(n, target) or _inside(_real(n), _real(target)):
                    return e
        elif any(os.path.basename(w) == e for w in words) or re.search(
                r"(?<![\w./-])" + re.escape(e) + r"(?![\w-])", raw):
            return e
    return ""


def _substitutions(cmd: str) -> Tuple[str, List[str]]:
    """cmd with each $( ) and ` ` replaced by a placeholder word, and their insides."""
    out, inner, i = [], [], 0
    quote = ""
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            out.append(c)
            if c == "'":
                quote = ""
            i += 1
            continue
        if c == "\\" and i + 1 < len(cmd):
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        if cmd.startswith("$(", i) or c == "`" or cmd.startswith("<(", i) or cmd.startswith(">(", i):
            if c == "`":
                j = cmd.find("`", i + 1)
                if j < 0:
                    raise Unclear("an open `")
                inner.append(cmd[i + 1:j])
                i = j + 1
            else:
                depth, j = 1, i + 2
                while j < len(cmd) and depth:
                    depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                    j += 1
                if depth:
                    raise Unclear("an open $(")
                inner.append(cmd[i + 2:j - 1])
                i = j
            out.append("__SUB__")
            continue
        out.append(c)
        i += 1
    return "".join(out), inner


def strip_heredocs(cmd: str) -> str:
    """The command without the bodies of its here-documents (text, not commands)."""
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        ends = [m.group(2) for m in HEREDOC.finditer(lines[i])]
        i += 1
        for end in ends:
            while i < len(lines) and lines[i].strip() != end:
                i += 1
            i += 1  # the end line itself
    return "\n".join(out)


def _split(cmd: str) -> List[List[str]]:
    lex = shlex.shlex(cmd.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = "#"
    segs, cur = [], []
    try:
        for tok in lex:
            if tok in REDIRS:
                cur.append(tok)
            elif set(tok) <= set(";&|()<>\n"):
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(tok)
    except ValueError as e:  # an open quote
        raise Unclear(str(e))
    if cur:
        segs.append(cur)
    return segs


def _branch(cwd: str) -> str:
    """The checked-out branch, read from .git/HEAD (no git process); "" when unknown."""
    d = Path(cwd)
    for p in [d, *d.parents]:
        g = p / ".git"
        try:
            if g.is_file():  # a worktree: "gitdir: <path>"
                g = Path(g.read_text().split(":", 1)[1].strip())
            head = (g / "HEAD").read_text().strip()
        except (OSError, IndexError):
            continue
        return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    return ""


def _git(args: List[str], cwd: str, ctx: dict) -> str:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            if args[i] == "-C":
                cwd = _norm(args[i + 1], cwd, ctx["home"])
            i += 2
        else:
            i += 1
    if i >= len(args):
        return ""
    sub, rest = args[i], args[i + 1:]
    shared = ctx["lists"]["shared"]
    here = ctx.get("branch_of", _branch)(cwd)

    def is_shared(b: str) -> bool:
        b = b.replace("refs/heads/", "")
        return b in shared or (b in ("", "HEAD") and (here in shared or not here))
    if sub == "push":
        force = any(a in ("-f", "--force", "--mirror") or a.startswith("--force-with-lease")
                    or a == "--force-if-includes" for a in rest)
        delete = any(a in ("-d", "--delete", "--prune") for a in rest)
        refs = [a for a in rest if not a.startswith("-")][1:]  # after the remote
        for r in refs:
            plus = r.startswith("+")
            src, _, dst = r.lstrip("+").partition(":")
            target = dst or src
            if (plus or force) and is_shared(target):
                return f"force push to {target or here or 'the current branch'}"
            if (r.startswith(":") or delete) and is_shared(target):
                return f"deleting the shared branch {target}"
        if force and not refs and is_shared(""):
            return f"force push to {here or 'the current branch'}"
        if "--mirror" in rest:
            return "push --mirror"
        return ""
    if sub in ("filter-branch", "filter-repo") and (here in shared or not here):
        return f"git {sub} on {here or 'an unknown branch'}"
    if here in shared or not here:
        on = here or "an unknown branch"
        if sub == "rebase" and not any(a in ("--abort", "--quit", "--show-current-patch")
                                       for a in rest):
            return f"rebase on {on}"
        if sub == "commit" and "--amend" in rest:
            return f"commit --amend on {on}"
        if sub == "reset" and [a for a in rest if not a.startswith("-") and a not in ("HEAD", "--")]:
            if not any(a == "--" for a in rest):
                return f"reset of {on} to another commit"
    if sub == "branch" and any(a in ("-f", "-D", "-M", "--force", "-C") for a in rest):
        if any(is_shared(a) for a in rest if not a.startswith("-")):
            return "moving or deleting a shared branch"
    if sub == "update-ref" and any(is_shared(a) for a in rest if a.startswith("refs/heads/")):
        return "update-ref on a shared branch"
    if sub in ("checkout", "switch") and any(a in ("-B", "-C") for a in rest):
        if any(is_shared(a) for a in rest if not a.startswith("-")):
            return "resetting a shared branch"
    return ""


def _targets(name: str, args: List[str]) -> Tuple[List[str], List[str]]:
    """(deleted, written) path words of one command."""
    plain = [a for a in args if not a.startswith("-") or a == "-"]
    if name in ("rm", "rmdir", "unlink", "shred", "srm", "trash", "trash-put"):
        return [a for a in plain if a != "-"], []
    if name == "gio":
        return (plain[1:], []) if plain[:1] in (["trash"], ["remove"]) else ([], [])
    if name == "mv":
        return plain[:-1], plain[-1:]
    if name in ("cp", "install", "ln"):
        return [], plain[-1:]
    if name in ("tee", "truncate", "touch", "mkdir", "chmod", "chown", "chgrp"):
        return [], plain
    if name == "dd":
        return [], [a[3:] for a in args if a.startswith("of=")]
    if name == "rsync":
        dst = plain[-1:]
        return (dst if any(a.startswith("--delete") or a.startswith("--remove-source")
                           for a in args) else []), dst
    if name in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a == "--in-place"
                                      for a in args):
        return [], plain[1:]
    return [], []


def _find(args: List[str]) -> Tuple[List[str], bool, List[str]]:
    """(start folders, deletes, the -exec command)."""
    starts, i = [], 0
    while i < len(args) and not args[i].startswith(("-", "(", "!")):
        starts.append(args[i])
        i += 1
    rest = args[i:]
    exe = []
    for j, a in enumerate(rest):
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            exe = [w for w in rest[j + 1:] if w not in (";", "+", "\\;")]
            break
    return starts or ["."], "-delete" in rest, exe


def check(cmd: str, ctx: dict, depth: int = 0) -> Tuple[str, str]:
    """(class, why) for the first night rule this shell command breaks; ("", "") when none.
    A command it cannot read (eval, bash -c "$X", xargs rm, a command name built from variables)
    is ("unclear", why) unless every class it could touch is allowed: never a guessed allow."""
    allowed = set(ctx.get("allowed", ()))
    if depth > 4:
        return "unclear", "commands nested too deep"
    cmd = strip_heredocs(cmd) if depth == 0 else cmd
    raw = cmd
    if any(own in raw for own in ctx.get("own", [])):
        return "rules", "the overnight files are changed by the developer only"
    try:
        flat, inner = _substitutions(cmd)
        for sub in inner:
            got = check(sub, ctx, depth + 1)
            if got[0]:
                return got
        segs = _split(flat)
    except Unclear as e:
        return _unclear(allowed, f"cannot read the command ({e})")
    cwd = ctx["cwd"]
    for seg in segs:
        got, cwd = _segment(seg, raw, {**ctx, "cwd": cwd}, allowed, depth)
        if got[0]:
            return got
    return "", ""


def _unclear(allowed: set, why: str) -> Tuple[str, str]:
    return ("", "") if allowed >= set(CLASSES) else ("unclear", why)


def _segment(seg: List[str], raw: str, ctx: dict, allowed: set, depth: int):
    """(result, cwd after it) for one simple command."""
    cwd = ctx["cwd"]
    words, writes, i = [], [], 0
    while i < len(seg):  # redirections out of the words
        t = seg[i]
        if t in REDIRS and not t.startswith("<"):
            if i + 1 < len(seg):
                writes.append(seg[i + 1])
            i += 2
            continue
        if t in REDIRS:
            i += 2
            continue
        words.append(t)
        i += 1
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if "hardware" not in allowed:
        hit = _hardware_hit(words + writes, raw, ctx)
        if hit:
            return ("hardware", f"{hit} is on the hardware list"), cwd
    for w in writes:
        if w in ("/dev/null", "/dev/stdout", "/dev/stderr") or w.startswith("&"):
            continue
        try:
            n = _norm(w, cwd, ctx["home"])
        except Unclear:
            if _protected(ctx) and "delete" not in allowed:
                return ("unclear", f"cannot tell where {w} is"), cwd
            continue
        if _own(n, ctx):
            return ("rules", "the overnight files are changed by the developer only"), cwd
        why = write_blocked(n, ctx)
        if why and "delete" not in allowed:
            return ("delete", why), cwd
    # wrappers: env, nice, timeout 5, ...
    while words:
        name = os.path.basename(words[0])
        if name in WRAPPERS:
            words = words[1:]
            while words and (words[0].startswith("-") or re.match(r"^[A-Za-z_]\w*=", words[0])
                             or re.match(r"^\d", words[0])):  # nice -n 5, timeout 30s
                words = words[1:]
            continue
        break
    if not words:
        return ("", ""), cwd
    first = words[0]
    if "$" in first or "__SUB__" in first or "`" in first:
        return _unclear(allowed, f"the command name {first} is built from variables"), cwd
    name, args = os.path.basename(first), words[1:]
    if name == "cd":
        try:
            return ("", ""), _norm(args[0] if args else ctx["home"], cwd, ctx["home"])
        except Unclear:
            return ("", ""), "/__unknown__"
    if name in ADMIN and "admin" not in allowed:
        return ("admin", f"{name} needs admin rights"), cwd
    if name == "kaptyorka" and "overnight" in args and "off" in args:
        return ("rules", "only the developer turns overnight mode off"), cwd
    if name == "eval":
        text = " ".join(args)
        if "$" in text or "__SUB__" in text:
            return _unclear(allowed, "eval of a variable or a command's output"), cwd
        return check(text, ctx, depth + 1), cwd
    if name in SHELLS or name in ("source", "."):
        if "-c" in args:
            body = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            if "$" in body or "__SUB__" in body:
                return _unclear(allowed, f"{name} -c with variables"), cwd
            return check(body, ctx, depth + 1), cwd
    if name == "xargs":
        k = 0
        while k < len(args) and args[k].startswith("-"):
            k += 2 if args[k] in ("-n", "-P", "-I", "-L", "-d", "-s", "-E", "-a") else 1
        inner = args[k:] or ["echo"]
        iname = os.path.basename(inner[0])
        if iname in ADMIN and "admin" not in allowed:
            return ("admin", f"xargs {iname} needs admin rights"), cwd
        if iname in DELETERS | WRITERS | {"git"} | SHELLS | {"eval"}:
            return _unclear(allowed, f"xargs {iname}: the targets come from input"), cwd
        return check(shlex.join(inner), ctx, depth + 1), cwd
    if name == "find":
        starts, deletes, exe = _find(args)
        if exe:
            ename = os.path.basename(exe[0])
            if ename in ADMIN and "admin" not in allowed:
                return ("admin", f"find -exec {ename} needs admin rights"), cwd
            got = check(shlex.join([w for w in exe if w != "{}"]), ctx, depth + 1)
            if got[0]:
                return got, cwd
            deletes = deletes or ename in DELETERS or (ename == "mv")
        if deletes and "delete" not in allowed:
            for s in starts:
                try:
                    why = delete_blocked(_norm(_glob_base(s), cwd, ctx["home"]), ctx)
                except Unclear:
                    return ("unclear", f"cannot tell where {s} is"), cwd
                if why:
                    return ("delete", why), cwd
        return ("", ""), cwd
    if name == "git" and "git" not in allowed:
        try:
            why = _git(args, cwd if cwd != "/__unknown__" else ctx["cwd"], ctx)
        except Unclear as e:
            return _unclear(allowed, f"cannot tell the git folder ({e})"), cwd
        if why:
            return ("git", why), cwd
    deleted, written = _targets(name, args)
    if "delete" not in allowed:
        for w in deleted:
            if cwd == "/__unknown__" and not w.startswith("/"):
                return ("unclear", f"cannot tell where {w} is after a cd"), cwd
            try:
                why = delete_blocked(_norm(_glob_base(w), cwd, ctx["home"]), ctx)
            except Unclear:
                return ("unclear", f"cannot tell what {w} deletes"), cwd
            if why:
                return ("delete", why), cwd
        for w in written:
            try:
                why = write_blocked(_norm(w, cwd, ctx["home"]), ctx)
            except Unclear:
                why = f"cannot tell where {w} is" if _protected(ctx) else ""
                if why:
                    return ("unclear", why), cwd
            if why:
                return ("delete", why), cwd
    for w in deleted + written:
        try:
            if _own(_norm(_glob_base(w), cwd, ctx["home"]), ctx):
                return ("rules", "the overnight files are changed by the developer only"), cwd
        except Unclear:
            pass
    return ("", ""), cwd


def context(st: dict, cwd: str) -> dict:
    home = os.path.expanduser("~")
    own = [str(paths.home() / "overnight"), str(paths.home() / "overnight.json")]
    return {"cwd": cwd or st.get("cwd") or home, "home": home, "allowed": st.get("allowed", []),
            "lists": lists(st.get("project", "")), "own": own}


def check_tool(tool: str, tool_input: dict, ctx: dict) -> Tuple[str, str]:
    """(class, why) for any tool call; ("", "") when the night rules let it through."""
    allowed = set(ctx.get("allowed", ()))
    if isinstance(tool_input.get("command"), str) and tool in ("Bash", "Monitor"):
        return check(tool_input["command"], ctx)
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(p, str):
            return "", ""
        try:
            n = _norm(p, ctx["cwd"], ctx["home"])
        except Unclear:
            return _unclear(allowed, f"cannot tell where {p} is")
        if _own(n, ctx):
            return "rules", "the overnight files are changed by the developer only"
        if "hardware" not in allowed and _hardware_hit([p, n], p, ctx):
            return "hardware", f"{p} is on the hardware list"
        why = write_blocked(n, ctx)
        if why and "delete" not in allowed:
            return "delete", why
    return "", ""


# ---------- the developer's ask rules (read only) ----------

def _settings_files(cwd: str) -> List[Path]:
    home = Path.home()
    out = [home / ".claude" / "settings.json"]
    for d in [Path(cwd), *Path(cwd).parents]:
        if (d / ".claude").is_dir() or (d / ".git").exists():
            out += [d / ".claude" / "settings.json", d / ".claude" / "settings.local.json"]
            break
    out += [Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    return out


def ask_rules(cwd: str) -> List[str]:
    rules = []
    for f in _settings_files(cwd):
        try:
            data = json.loads(f.read_text())
            ask = data.get("permissions", {}).get("ask", [])
        except (OSError, ValueError, AttributeError):
            continue
        rules += [r for r in ask if isinstance(r, str)] if isinstance(ask, list) else []
    return rules


def asks(tool: str, tool_input: dict, rules: List[str]) -> str:
    """The developer's ask rule this call matches, "" when none. Unsure counts as a match."""
    for rule in rules:
        m = re.fullmatch(r"\s*([^()]+?)\s*(?:\((.*)\))?\s*", rule)
        if not m:
            continue
        name, spec = m.group(1), m.group(2)
        if name.startswith("mcp__"):
            if tool == name or tool.startswith(name + "__"):
                return rule
            continue
        if name != tool:
            continue
        if spec in (None, "", "*"):
            return rule
        if tool == "Bash":
            cmd = str(tool_input.get("command", ""))
            pat = spec[:-2] + "*" if spec.endswith(":*") else spec
            parts = [cmd] + [p.strip() for p in re.split(r"&&|\|\||;|\||\n", cmd)]
            if any(fnmatch.fnmatchcase(p, pat) or p == spec for p in parts):
                return rule
            continue
        target = str(tool_input.get("file_path") or tool_input.get("url") or tool_input.get("path")
                     or "")
        if spec.startswith("domain:"):
            if spec[7:] in target:
                return rule
            continue
        pat = spec.lstrip("/")
        if pat.startswith("~"):
            pat = os.path.expanduser(pat).lstrip("/")
        if fnmatch.fnmatchcase(target.lstrip("/"), pat) or fnmatch.fnmatchcase(target, "*" + pat):
            return rule
    return ""


# ---------- on, off and the words of the developer ----------

def triggered(prompt: str) -> bool:
    return bool(TRIGGER.search(prompt or ""))


def turn_on(sid: str, project: str, cwd: str, team: bool, key: str, machine: str,
            task: str, mode: str, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    st = {"on": True, "started": now, "project": project, "cwd": cwd, "team": team, "key": key,
          "machine": machine, "task": safe(task, 120), "mode": mode, "allowed": [],
          "setup": True, "nudges": 0, "resumes": 0, "waiting": None, "pushed": 0,
          "last": now, "resumed_at": 0}
    save(sid, st)
    note(sid, "event", "overnight mode on", now)
    return st


def turn_off(sid: str, why: str, now: Optional[float] = None) -> Optional[dict]:
    """Off, with the reason in the log; the state as it was (None when it was not on)."""
    st = state(sid)
    if st is None:
        return None
    now = time.time() if now is None else now
    note(sid, "event", f"overnight mode off: {why}", now)
    save(sid, {**st, "on": False, "ended_at": now, "why": why})
    return st


def apply_words(sid: str, st: dict, prompt: str) -> List[str]:
    """The developer's changes in plain words; one line repeated back for each."""
    lines = []
    for raw in re.split(r"\n|;|\.\s+|\.$", prompt or ""):
        m = MAY.search(raw)
        if m:
            for cls, pat in ALLOW:
                if pat.search(m.group(1)) and cls not in st["allowed"]:
                    st["allowed"].append(cls)
                    lines.append(f"Overnight: {LABEL[cls]} {'are' if cls == 'hardware' else 'is'}"
                                 f" allowed for this task.")
                    break
        lm = LIST.search(raw)
        if lm:
            what, items = lm.group(1).lower(), [x.strip() for x in re.split(r",\s*", lm.group(2))
                                                if x.strip()]
            kind = ("hardware" if what.startswith("hardware") else
                    "shared" if what.startswith("shared") else "protected")
            machine = "machine" in raw.lower() or (kind == "protected" and "project" not in raw.lower())
            c = config()
            where = c.setdefault("machine", {}) if machine else \
                c.setdefault("projects", {}).setdefault(st.get("project", ""), {})
            have = where.setdefault(kind, [])
            new = [i for i in items if i not in have]
            have += new
            save_config(c)
            if new:
                scope = "this machine" if machine else f"project {st.get('project')}"
                lines.append(f"Overnight: {', '.join(new)} added to the {kind} list of {scope}.")
    if lines:
        save(sid, st)
        for line in lines:
            note(sid, "event", line)
    return lines


def is_reply(prompt: str) -> bool:
    """A prompt that only answers the night rules: ok, or changes in words."""
    p = (prompt or "").strip()
    if YES.fullmatch(p):
        return True
    parts = [x for x in re.split(r"\n|;|\.\s+|\.$", p) if x and x.strip()]
    return bool(parts) and all(MAY.search(x) or LIST.search(x) or YES.fullmatch(x) for x in parts)


def _head(text: str) -> str:
    return " ".join(str(text).split())[:200]


def delivered(sid: str, text: str, now: Optional[float] = None) -> None:
    """Called before kaptyorka delivers a text to the session: it never counts as a person."""
    now = time.time() if now is None else now
    f = _dir() / f"{_sid(sid)}.sent"
    keep = []
    try:
        for raw in f.read_text().splitlines():
            try:
                d = json.loads(raw)
                if now - float(d["t"]) < SENT_KEEP_S:
                    keep.append(raw)
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    keep.append(json.dumps({"t": now, "head": _head(text)}))
    _private(f, "\n".join(keep) + "\n")


def _sent(sid: str) -> List[str]:
    try:
        lines = (paths.home() / "overnight" / f"{_sid(sid)}.sent").read_text().splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw)["head"])
        except (ValueError, KeyError, TypeError):
            continue
    return [h for h in out if h]


def _origin(transcript: str, prompt_id: str) -> str:
    """The transcript's origin kind for this prompt ("human", "peer", ...), "" when not written
    yet. Claude Code records it on each user line (seen in 2.1.32x transcripts; not in the
    hooks docs, so only a second signal)."""
    if not transcript or not prompt_id:
        return ""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262144))
            tail = f.read().decode(errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(tail):
        if prompt_id not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") == "user" and d.get("promptId") == prompt_id:
            o = d.get("origin")
            if isinstance(o, dict) and isinstance(o.get("kind"), str):
                return o["kind"]
    return ""


def from_person(sid: str, data: dict) -> bool:
    """Whether this UserPromptSubmit prompt was typed by a person. Claude Code's hook input does
    not say who sent a prompt (docs/en/hooks#userpromptsubmit), so: a text kaptyorka delivered,
    or one marked as from another session, is not a person; the transcript's origin, when
    already written, decides; anything else counts as a person (off is the safe side: the
    session's own permission prompts come back)."""
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
    full = " ".join(prompt.split())
    if any(h in full for h in _sent(sid)):
        return False
    if prompt.lstrip().startswith("Another Claude session sent a message"):
        return False
    kind = _origin(str(data.get("transcript_path") or ""), str(data.get("prompt_id") or ""))
    if kind:
        return kind == "human"
    return True


def rules_text(st: dict) -> str:
    lst = lists(st.get("project", ""))
    hw = ", ".join(lst["hardware"]) or "none set"
    prot = ", ".join(lst["protected"]) or "none set"
    rows = [
        ("admin", "sudo, su, pkexec, doas"),
        ("git", f"force push or history rewrite on {', '.join(lst['shared'])}"),
        ("delete", f"deleting system folders, the home folder, other users' folders,"
                   f" or protected folders ({prot})"),
        ("hardware", f"hardware commands ({hw})"),
    ]
    out = ["Overnight rules for this task:",
           "  Allowed: everything you could do yourself, except:"]
    for i, (cls, text) in enumerate(rows, 1):
        out.append(f"  {i}. {text}: {'allowed for this task' if cls in st['allowed'] else 'blocked'}")
    out.append("  Plus the team and project rules, and your own Claude Code permission rules.")
    if st.get("mode") == "plan":
        out.append("  Warning: this session is in plan mode. Edits wait for a person there, so"
                   " the task would stall. Leave plan mode (shift+tab) before you go.")
    out.append('Reply "ok", or change it in words, e.g. "you may move the motors",'
               ' "hardware: motorctl, /dev/ttyUSB0", "protected folders: ~/recordings".')
    return "\n".join(out)


def instructions(st: dict) -> str:
    """What Claude hears at turn-on (additionalContext)."""
    return (
        "kaptyorka overnight mode is on for this session. Show the user this list word for word"
        " before you start, then start the task without waiting:\n\n" + rules_text(st) + "\n\n"
        "From now on nobody is around until morning. Never ask the user a question: when you must"
        " choose, take the option you would recommend and record it with"
        " `kaptyorka overnight note decision \"<the question and your choice>\"`. A blocked or"
        " denied step: skip it, record it with `kaptyorka overnight note skipped \"<what>\"`, and"
        " go on with the rest. Record finished parts with `kaptyorka overnight note done"
        " \"<what>\"`. The team and project rules still bind you: when one forbids a step the"
        " night rules allow, skip it and run `kaptyorka overnight clash --scope team|project"
        " --rule \"<rule title>\" --class admin|git|delete|hardware|other`. Never put secrets in"
        f" notes. When the whole task is finished, end your last message with {DONE_MARK}.")


# ---------- the morning report ----------

def report(sid: str, st: dict, why: str, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    items = notes(sid)

    def hm(t):
        return time.strftime("%H:%M", time.localtime(t))

    def section(title, kind):
        rows = [f"  - {n['text']}" for n in items if n.get("kind") == kind]
        return [title, *rows] if rows else []
    where = ", ".join(x for x in (st.get("machine"), st.get("project")) if x)
    task = st.get("task") or "(no title)"
    out = [f'Overnight task: "{task}"' + (f"  ({where})" if where else ""),
           f"{hm(st['started'])} to {hm(now)}, {why}.", ""]
    for title, kind in (("Done", "done"), ("Decisions made for you", "decision"),
                        ("Skipped, needs your yes", "skipped"), ("Rule clashes", "clash")):
        s = section(title, kind)
        if s:
            out += s
    ev = [f"  - {hm(n['t'])} {n['text']}" for n in items if n.get("kind") == "event"]
    if ev:
        out += ["Events", *ev]
    return "\n".join(out).rstrip() + "\n"


def save_report(sid: str, text: str, now: Optional[float] = None) -> Path:
    now = time.time() if now is None else now
    d = _dir() / "reports"
    d.mkdir(mode=0o700, exist_ok=True)
    f = d / f"{time.strftime('%Y-%m-%d', time.localtime(now))}-{_sid(sid)[:8]}.md"
    _private(f, text)
    return f


# ---------- the hook side ----------

DENY_QUESTION = ("No human until morning (kaptyorka overnight mode). Do not ask: take the option"
                 " you recommend, record it with `kaptyorka overnight note decision \"...\"`, and"
                 " go on.")


def _skip(cls: str, why: str) -> str:
    return (f"Blocked by the overnight rules ({LABEL.get(cls, cls)}: {why}). Skip this step,"
            f" record it with `kaptyorka overnight note skipped \"...\"`, and go on with the rest.")


def _deny_pre(reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


def _decide(behavior: str, message: str = "") -> str:
    d = {"behavior": behavior}
    if message:
        d["message"] = message
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}})


def _expired(sid: str, st: dict, now: float, finish: Callable) -> bool:
    if now - float(st.get("started", now)) > CAP_S:
        finish(sid, "stopped by the 16-hour cap")
        return True
    return False


def on_pretool(sid: str, data: dict, finish: Callable, now: Optional[float] = None) -> str:
    st = state(sid)
    if st is None:
        return ""
    now = time.time() if now is None else now
    if _expired(sid, st, now, finish):
        return ""
    tool = str(data.get("tool_name") or "")
    ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if st.get("nudges") or st.get("waiting") or now - st.get("last", 0) > 60:
        save(sid, {**st, "nudges": 0, "waiting": None, "last": now})  # it works again
    if tool == "AskUserQuestion":
        qs = [q.get("question", "") for q in ti.get("questions", []) if isinstance(q, dict)]
        note(sid, "event", f"question denied: {'; '.join(qs)[:200]}", now)
        return _deny_pre(DENY_QUESTION)
    cls, why = check_tool(tool, ti, context(st, str(data.get("cwd") or "")))
    if cls:
        note(sid, "skipped", f"{_what(tool, ti)} ({LABEL.get(cls, cls)})", now)
        return _deny_pre(_skip(cls, why))
    return ""


def _what(tool: str, ti: dict) -> str:
    if isinstance(ti.get("command"), str):
        return ti["command"][:160]
    return f"{tool} {ti.get('file_path') or ''}".strip()


def on_permission(sid: str, data: dict, finish: Callable, now: Optional[float] = None) -> str:
    """A permission prompt with nobody to answer it: inside the night rules allow, outside (or
    an ask rule of the developer's) deny "skip and note". Deny rules never reach here as an
    allow: Claude Code applies them first."""
    st = state(sid)
    if st is None:
        return ""
    now = time.time() if now is None else now
    if _expired(sid, st, now, finish):
        return ""
    tool = str(data.get("tool_name") or "")
    ti = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    cwd = str(data.get("cwd") or st.get("cwd") or "")
    if tool in ("AskUserQuestion", "ExitPlanMode"):
        return _decide("deny", DENY_QUESTION)
    cls, why = check_tool(tool, ti, context(st, cwd))
    if cls:
        note(sid, "skipped", f"{_what(tool, ti)} ({LABEL.get(cls, cls)})", now)
        return _decide("deny", _skip(cls, why))
    rule = asks(tool, ti, ask_rules(cwd))
    if rule:
        note(sid, "skipped", f"{_what(tool, ti)} (your ask rule {rule})", now)
        return _decide("deny", f"The developer's own settings ask a person before this ({rule})."
                               " Skip this step, record it with `kaptyorka overnight note skipped"
                               " \"...\"`, and go on.")
    return _decide("allow")


def on_stop(sid: str, data: dict, finish: Callable, now: Optional[float] = None) -> Optional[str]:
    """None when overnight mode is off (the hub's done check then runs); "" lets it stop."""
    st = state(sid)
    if st is None:
        return None
    now = time.time() if now is None else now
    if _expired(sid, st, now, finish):
        return ""
    last = str(data.get("last_assistant_message") or "")
    if DONE_MARK in last:
        finish(sid, "finished")
        return ""
    busy = [t for t in data.get("background_tasks") or [] if isinstance(t, dict)]
    if busy or data.get("session_crons"):
        return ""  # it waits for its own background work, which wakes it
    if st.get("nudges", 0) >= NUDGES:
        if st.get("nudges") == NUDGES:
            note(sid, "event", f"stopped; no more continue after {NUDGES} in a row", now)
            save(sid, {**st, "nudges": NUDGES + 1})
        return ""
    save(sid, {**st, "nudges": st.get("nudges", 0) + 1, "last": now})
    return json.dumps({"decision": "block", "reason": (
        "Overnight task: nobody is here until morning, so keep going. If a step is blocked, skip"
        " it, note it, and continue with the rest. When everything is done, end your message"
        f" with {DONE_MARK}.")})


def on_failure(sid: str, data: dict, now: Optional[float] = None) -> None:
    st = state(sid)
    if st is None:
        return
    now = time.time() if now is None else now
    err = str(data.get("error") or "unknown")
    if not st.get("waiting"):
        note(sid, "event", "usage limit; waiting" if err == "rate_limit" else f"API error ({err});"
             " waiting", now)
        save(sid, {**st, "waiting": {"since": now, "error": err}, "pushed": 0})


def on_end(sid: str, data: dict, finish: Callable) -> None:
    if state(sid) is not None:
        finish(sid, f"the session ended ({data.get('reason') or 'exit'})")


# ---------- the bridge side ----------

CONTINUE = ("kaptyorka: continue the overnight task where it stopped. Nobody is here until"
            f" morning. When everything is done, end your message with {DONE_MARK}.")


def watch(alive: Callable[[str], bool], deliver: Callable[[str, str], None], resume: Callable,
          finish: Callable, now: Optional[float] = None) -> None:
    """One pass, from the bridge: the cap, a crashed session (resume, bounded), a wait after a
    usage limit or an API error ("continue", bounded)."""
    now = time.time() if now is None else now
    for sid, st in active():
        try:
            if _expired(sid, st, now, finish):
                continue
            if not alive(sid):
                if now - st.get("resumed_at", 0) < RESUME_GRACE_S:
                    continue
                if st.get("resumes", 0) >= RESUMES:
                    finish(sid, f"the session crashed; not restarted after {RESUMES} restarts")
                    continue
                ok, line = resume(sid, st)
                note(sid, "event", f"the session crashed; {line}", now)
                save(sid, {**st, "resumes": st.get("resumes", 0) + 1, "resumed_at": now,
                           "waiting": None})
                if not ok:
                    finish(sid, "the session crashed and could not be restarted")
                continue
            w = st.get("waiting")
            if isinstance(w, dict):
                since = float(w.get("since", now))
                if now - since > WAIT_S:
                    note(sid, "event", "gave up waiting after 6 hours", now)
                    save(sid, {**st, "waiting": None})
                    continue
                gap = PUSH_GAP_S.get(str(w.get("error")), PUSH_GAP_OTHER_S)
                if now - max(since, float(st.get("pushed") or 0)) >= gap:
                    delivered(sid, CONTINUE, now)
                    try:
                        deliver(sid, CONTINUE)
                        note(sid, "event", "sent continue after the wait", now)
                    except Exception as e:
                        note(sid, "event", f"could not send continue: {type(e).__name__}", now)
                    save(sid, {**st, "pushed": now})
        except Exception:
            continue  # one session must not stop the others


def resume_argv(sid: str, st: dict) -> List[str]:
    """tmux, detached, in the task's folder: `claude --resume <id> <continue>`. Claude Code keeps
    the permission mode on resume (docs/en/sessions); nothing skips permissions."""
    name = "kap-night-" + _sid(sid)[:8]
    return ["tmux", "new-session", "-d", "-s", name, "-c", st.get("cwd") or os.path.expanduser("~"),
            "claude", "--resume", _sid(sid), CONTINUE]


def status_line(sid: str) -> str:
    st = state(sid)
    if st is None:
        return "overnight mode is off for this session; /hub overnight turns it on"
    allowed = ", ".join(LABEL[c] for c in st["allowed"]) or "nothing extra"
    return (f"overnight mode is on (since {time.strftime('%H:%M', time.localtime(st['started']))};"
            f" allowed for this task: {allowed}). To stop it: /hub overnight off")


# ---------- finishing, and the hub for team sessions ----------

def post_hub(st: dict, body: dict, timeout: float = 3) -> bool:
    """POST /api/overnight for a team session; personal sessions never call it."""
    if not st.get("team"):
        return False
    from kaptyorka import http
    from kaptyorka.identity import Identity

    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):
        return False
    ident = Identity(st.get("key", ""), cfg.get("machine", ""), "claude-code",
                     st.get("project", ""), st.get("cwd", ""))
    try:
        status, _ = http.request("POST", f"{cfg['hub']}/api/overnight", cfg["token"], body,
                                 headers=ident.headers(), timeout=timeout)
    except Exception:
        return False
    return status == 200


def finish(sid: str, why: str, now: Optional[float] = None, post: Callable = post_hub) -> str:
    """Off, with the morning report: saved here; for a team session also to the person's hub
    page. The report text ("" when it was not on)."""
    st = turn_off(sid, why, now)
    if st is None:
        return ""
    text = report(sid, st, why, now)
    save_report(sid, text, now)
    if st.get("team"):
        post(st, {"kind": "report", "text": text})
    return text


def clash(sid: str, scope: str, rule: str, cls: str, now: Optional[float] = None,
          post: Callable = post_hub) -> str:
    """A team or project rule forbade what the night rules allowed: the local log, and for a
    team session a short fixed-text event on the hub (project, scope, rule title, class)."""
    st = state(sid)
    if st is None:
        return "kaptyorka: overnight mode is off for this session"
    scope = scope if scope in ("team", "project") else "team"
    cls = cls if cls in CLASSES else "other"
    title = safe(rule.replace('"', "'"), 80) or "(a rule)"
    what = LABEL.get(cls, "a step")
    note(sid, "clash", f"the night rules allowed {what}, but the {scope} rule \"{title}\""
                       f" forbids it: skipped", now)
    sent = post(st, {"kind": "clash", "scope": scope, "rule": title, "class": cls}) \
        if st.get("team") else False
    return "kaptyorka: clash noted" + (" and sent to the manager" if sent else "")


# ---------- the hub side (fixed texts only) ----------

REPORT_CAP = 6000
CLASH_KIND = "overnight-clash"
CLASH_RE = re.compile(r'(team|project) rule "([^"]{1,80})" forbade (admin|git|delete|hardware|other)'
                      r" in (.{1,100})")


def clash_detail(scope: str, rule: str, cls: str, project: str) -> str:
    """The log line: scope, rule title, class and project, nothing else."""
    scope = scope if scope in ("team", "project") else "team"
    cls = cls if cls in CLASSES else "other"
    title = safe(rule.replace('"', "'"), 80) or "(a rule)"
    return f'{scope} rule "{title}" forbade {cls} in {safe(project, 100) or "(project)"}'


def clash_notice(person: str, detail: str) -> str:
    """What the manager hears; "" for a line not made by clash_detail."""
    m = CLASH_RE.fullmatch(detail or "")
    if not m:
        return ""
    scope, title, cls, project = m.groups()
    what = LABEL.get(cls, "a step")
    return (f"overnight rule clash: {person}'s overnight session in {project} skipped {what},"
            f" because the {scope} rule \"{title}\" forbids it. Tell {person} once, in one line"
            f" with `send` to their session or page: \"Your overnight task allowed {what}, but the"
            f" {scope} rule '{title}' forbids it, so the session skipped it.\"")
