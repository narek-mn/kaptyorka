"""The manager knows itself: a can / cannot table from the hub's real tools, the lessons it
keeps (its own `lesson` tool and the admin's CLI), and step-by-step playbooks."""
from helpers import call, call_error, person, run
from test_hub_manager import admin_bridge, clock, hub, manager_online, orders  # noqa: F401

from kaptyorka import http, manager
from kaptyorka.hub import MANAGER_FIRST


def test_capability_table_comes_from_the_real_tools(hub):
    admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    md = (hub.folder / "CLAUDE.md").read_text()
    table = md.split("## What you can and cannot do")[1].split("\n## ")[0]
    can, cannot = table.split("You cannot:")
    for tool in ("door_request", "resume_session", "close_worker", "lesson", "work_decide"):
        assert f"- `{tool}`: " in can
    for tool in ("knock", "answer", "door_decide"):
        assert f"`{tool}`" not in can and f"- `{tool}`: " in cannot
    assert "check this list and load the tool with ToolSearch" in table
    assert "run commands, read or edit files" in cannot
    assert "## Playbooks" in md and "A session others depend on stopped" in md


def test_a_new_tool_rewrites_the_table(hub, clock):
    admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    first = (hub.folder / "CLAUDE.md").read_text()
    tools = [("brand_new", "Do a new thing. More words.")]
    assert "- `brand_new`: Do a new thing." in manager.claude_md("", tools)
    assert first != manager.claude_md("", tools)


def test_the_manager_records_lessons_and_a_new_run_gets_them(hub, clock):
    mgr, me = manager_online(hub)
    assert run(call(mgr, me, "lesson", text="never promise to close a worker you did not start")) \
        == "lesson saved (1 in all); your next run starts with them"
    clock[0] += 86400
    run(call(mgr, me, "lesson", text="answer in one line"))
    _, admin = person(hub.store, "root", "admin")
    assert http.request("POST", hub.url + "/api/lessons", admin,
                        {"text": "check who before answering"})[0] == 200
    text = http.request("GET", hub.url + "/api/lessons", admin)[1]
    assert [ln.split(": ", 1)[1] for ln in text.splitlines()] == [
        "check who before answering", "answer in one line",
        "never promise to close a worker you did not start"]
    assert "(root)" in text.splitlines()[0] and "(manager)" in text.splitlines()[1]
    first = manager.first_message(MANAGER_FIRST, None, hub.mm.lessons())
    assert first.startswith(MANAGER_FIRST + "\n\nLessons from corrections people gave you")
    assert first.endswith("never promise to close a worker you did not start")


def test_lessons_are_capped_at_forty_lines_newest_first():
    lessons = "\n".join(f"- lesson {i}" for i in range(60))
    out = manager.first_message("x", "tasks", lessons)
    assert out.count("- lesson") == 40 and "- lesson 0" in out and "- lesson 40" not in out
    assert out.index("tasks") < out.index("- lesson 0")


def test_only_the_manager_records_its_lessons(hub):
    _, token = person(hub.store, "alice")
    from types import SimpleNamespace
    alice = SimpleNamespace(url=hub.url, token=token)
    ident = {"key": "k:a", "machine": "box-1", "tool": "codex", "project": "proj-a"}
    assert "only the manager" in call_error(alice, ident, "lesson", text="x")
    assert http.request("POST", hub.url + "/api/lessons", token, {"text": "x"})[0] == 403


def test_the_launch_carries_the_lessons(hub, clock):
    b = admin_bridge(hub)
    hub.mm.add_lesson("say who was told", "root")
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    assert o["first_message"].endswith("say who was told")


def test_the_manager_runs_on_the_pinned_model_and_effort(hub, clock):
    b = admin_bridge(hub)
    _, admin = person(hub.store, "boss", "admin")
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    assert (o["model"], o["effort"]) == ("claude-opus-5-5", "medium")
    status, body = http.request("POST", hub.url + "/api/settings", admin,
                                {"manager_model": "gpt-9"})
    assert status == 400 and "use one of: claude-opus-5-5, opus, sonnet, haiku, fable" in body
    status, body = http.request("POST", hub.url + "/api/settings", admin,
                                {"manager_effort": "turbo"})
    assert status == 400 and "low, medium, high, xhigh, max" in body
    assert http.request("POST", hub.url + "/api/settings", admin,
                        {"manager_model": "sonnet", "manager_effort": "high"})[1] \
        ["manager_model"] == "sonnet"
    assert (hub.mm.model(), hub.mm.effort()) == ("sonnet", "high")
