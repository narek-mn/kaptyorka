// kaptyorka in an OpenCode 2 box chat (secure design v2, slice 5). Root-owned, read-only in the
// box: /etc/opencode/plugins/kaptyorka.js, the managed system folder ("Managed settings have the
// highest precedence", opencode.ai/docs/config). It runs in the box user's own OpenCode service.
// It tells the box helper about each chat and puts the standing rule, the team brief and unread
// messages into the chat's context. It never sends the prompt's text.
import { execFile } from "node:child_process";
const PY = "/usr/bin/python3";
const HELPER = ["-I", "/opt/kaptyorka/box/current/helper.py", "hook", "opencode"];
const hook = (event, sessionID, cwd) => new Promise((resolve) => {
  try {
    const child = execFile(PY, [...HELPER, event], { cwd, timeout: 8000 },
      (err, out) => resolve(err ? "" : String(out || "").trim()));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ session_id: sessionID, cwd, hook_event_name: event }));
  } catch {
    resolve("");
  }
});
export default {
  id: "kaptyorka",
  setup: async (ctx) => {
    const directory = ctx.location.directory;
    const brief = {}, turn = {};
    await ctx.session.hook("prompt", async (event) => {
      const sid = event.sessionID;
      if (!(sid in brief)) brief[sid] = await hook("start", sid, directory);
      turn[sid] = await hook("prompt", sid, directory);   // unread messages, if any
    });
    await ctx.session.hook("context", async (event) => {
      const sid = event.sessionID;
      for (const text of [brief[sid], turn[sid]]) if (text) event.system.push({ type: "text", text });
    });
  },
};
