// kaptyorka in an OpenCode 1 box chat (secure design v2, slice 5). Root-owned, read-only in the
// box, loaded from the managed /etc/opencode/opencode.json ("plugin"), which outranks every
// user and project file (opencode.ai/v1/docs/config: "Managed config files ... admin-controlled").
// It runs inside the chat's own window. It tells the box helper about the chat and puts the
// standing rule, the team brief and unread messages into its context; it never sends the prompt.
// Its file-tool guard holds the "nothing outside this team folder" rule even for an agent a
// project defines with its own permissions (measured on 1.19.6: an agent's rules come after the
// managed ones and win), the way the plugins guide blocks .env reads (throw in
// tool.execute.before, opencode.ai/v1/docs/plugins).
import { execFile } from "node:child_process";
import { realpathSync } from "node:fs";
import path from "node:path";
const PY = "/usr/bin/python3";
const HELPER = ["-I", "/opt/kaptyorka/box/current/helper.py", "hook", "opencode"];
const hook = (event, sessionID, cwd, title) => new Promise((resolve) => {
  try {
    const child = execFile(PY, [...HELPER, event], { cwd, timeout: 8000 },
      (err, out) => resolve(err ? "" : String(out || "").trim()));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ session_id: sessionID, cwd, hook_event_name: event, title: title || "" }));
  } catch {
    resolve("");
  }
});
const real = (p) => {
  let cur = p;
  for (let i = 0; i < 64; i++) {   // the nearest part that exists, resolved; the rest as written
    try { return path.join(realpathSync(cur), path.relative(cur, p)); } catch {}
    const up = path.dirname(cur);
    if (up === cur) break;
    cur = up;
  }
  return p;
};
const FILE_TOOLS = { read: "filePath", edit: "filePath", write: "filePath", patch: "filePath",
                     multiedit: "filePath", list: "path", glob: "path", grep: "path" };
export const KaptyorkaBox = async ({ directory }) => {
  const root = real(directory);
  const memory = path.join(path.dirname(root), "_memory");
  const inside = (p) => {
    const r = real(path.resolve(root, p));
    return [root, memory].some((top) => r === top || r.startsWith(top + path.sep));
  };
  const brief = {}, turn = {};
  return {
    "chat.message": async (input) => {
      const sid = input.sessionID;
      if (!(sid in brief)) brief[sid] = await hook("start", sid, directory);
      turn[sid] = await hook("prompt", sid, directory);
    },
    "experimental.chat.system.transform": async (input, output) => {
      for (const text of [brief[input.sessionID], turn[input.sessionID]]) if (text) output.system.push(text);
    },
    "tool.execute.before": async (input, output) => {
      const key = FILE_TOOLS[input.tool];
      const p = key && output && output.args ? output.args[key] : undefined;
      if (typeof p === "string" && p && !inside(p))
        throw new Error("kaptyorka: a team chat works only inside its team folder; " + p + " is outside it");
    },
    event: async ({ event }) => {
      if (event.type === "session.deleted" && event.properties && event.properties.info)
        await hook("end", event.properties.info.id, directory);
    },
  };
};
