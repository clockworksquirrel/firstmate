import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

// Same native before-hook contract as fm-primary-pretool-check.js: input owns
// tool/sessionID; output.args is the actual pending invocation. Throw to block.
// FM_ROOT_OVERRIDE must point to FirstMate's code root when copied into a worker.
// FM_HOME selects private state independently. No SDK or permission API is used.
function check(helper, env, payload) {
  return new Promise((finish) => {
    const child = spawn("python3", [helper, "check"], {
      env: { ...env, PYTHONDONTWRITEBYTECODE: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let overflow = false;
    child.stdout.on("data", (chunk) => {
      if (stdout.length + chunk.length > 16384) overflow = true;
      else stdout += chunk.toString();
    });
    // Diagnostics are deliberately generic; no child output is echoed to chat.
    child.stderr.resume();
    child.stdin.on("error", () => {});
    child.on("error", () => finish({ code: 2 }));
    child.on("close", (code) => {
      try {
        finish({ code: overflow ? 2 : code, result: JSON.parse(stdout) });
      } catch {
        finish({ code: 2 });
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export const FmHandsfreeTool = async () => {
  // Capture launch identity, including FM_TASK_ID. Arguments cannot change it.
  const env = { ...process.env };
  const root = resolve(env.FM_ROOT_OVERRIDE || fileURLToPath(new URL("../../", import.meta.url)));
  env.FM_HOME = resolve(env.FM_HOME || root);
  const helper = resolve(root, "bin/fm-handsfree-tool.py");
  return {
    "tool.execute.before": async (input, output) => {
      const { code, result } = await check(helper, env, {
        session_id: input?.sessionID,
        task_id: env.FM_TASK_ID || "",
        tool: input?.tool,
        args: output?.args,
      });
      if (code === 0 && result?.status === "allow") return;
      const id = result?.request_id;
      if (typeof id === "string" && /^[0-9a-f]{32}$/.test(id)) {
        if (["denied", "expired", "consumed"].includes(result.status)) {
          throw new Error(
            `HandsFreeBridge request ${id} is ${result.status}. ` +
            "Do not retry this action or resolve this record. " +
            "Report this terminal outcome to FirstMate and stop that action. " +
            "Native OpenCode permissions and OS/platform controls still apply.",
          );
        }
        if (result.status === "pending") throw new Error(
          `HandsFreeBridge request ${id} is pending. ` +
          `Report needs-decision [key=handsfree-${id}] through FirstMate. ` +
          `FirstMate must read FM_HOME/state/handsfree-tools/requests/${id}.json, ` +
          "ask the user in its own conversation, and resolve allow/deny with the user's exact words. " +
          "Replace {payload} in the primary-only command below with a JSON object containing " +
          "request_id, decision (allow or deny), and user_words. " +
          "An allow grants one retry with the identical session, task, tool and args. " +
          "Native OpenCode permissions and OS/platform controls still apply.\n" +
          result.resolver_command,
        );
      }
      throw new Error("HandsFreeBridge could not validate this action. Report needs-decision through FirstMate.");
    },
  };
};
