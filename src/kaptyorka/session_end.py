"""Why a Claude Code session stopped, told to the hub by its own hooks, so the team knows that
a session others depend on hit its usage limit (and when it resets) or ended, and the manager
can resume it (hub `resume_session`). Team sessions only; a personal one sends nothing.

Claude Code's StopFailure hook input has `error` ("rate_limit" for a usage limit), and the
optional `error_details` and `last_assistant_message`, the error text the person sees
(code.claude.com/docs/en/hooks#stopfailure). No field carries the reset time, so it is read
from that text when it says one ("resets 7:30pm", "resets at 19:30"); otherwise it is unknown.
SessionEnd has `reason` (clear, resume, logout, prompt_input_exit, other)."""
import re
import time
from typing import Optional

MAX_TEXT = 200
_RESET = re.compile(r"\breset(?:s|ting)?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?",
                    re.I)


def reset_at(text: str, now: Optional[float] = None) -> Optional[float]:
    """The next time of day `text` names after "resets", on this machine's clock; None when
    it names none."""
    m = _RESET.search(text or "")
    if m is None:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if half.startswith("p") and hour < 12:
        hour += 12
    elif half.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    now = time.time() if now is None else now
    t = time.localtime(now)
    at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))
    return at if at > now else at + 86400


def event(hook_event: str, data: dict, now: Optional[float] = None) -> Optional[dict]:
    """The body for POST /api/session-event, or None when there is nothing to say."""
    if hook_event == "failure":
        error = str(data.get("error") or "unknown")[:40]
        text = " ".join(str(data.get(k) or "") for k in ("last_assistant_message", "error_details"))
        return {"kind": "failure", "error": error, "text": " ".join(text.split())[:MAX_TEXT],
                "resets_at": reset_at(text, now) if error == "rate_limit" else None}
    if hook_event == "end":
        reason = str(data.get("reason") or "other")[:40]
        if reason in ("clear", "resume"):  # the session goes on under another id: no end
            return None
        return {"kind": "end", "reason": reason}
    return None


def line(name: str, error: str, reason: str, resets_at: Optional[float]) -> str:
    """What the hub says about a session that stopped: "box-1/claude-code/proj-a hit its
    usage limit, resets 19:30"."""
    if error == "rate_limit":
        when = time.strftime("%H:%M", time.localtime(resets_at)) if resets_at else "at an unknown time"
        return f"{name} hit its usage limit, resets {when}"
    if error:
        return f"{name} stopped on an API error ({error})"
    return f"{name} ended ({reason or 'exit'})"
