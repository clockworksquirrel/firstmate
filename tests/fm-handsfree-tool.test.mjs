import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, copyFile, mkdir, readFile, readdir, stat, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [root, temporary] = process.argv.slice(2);
assert.ok(root && temporary, "run through fm-handsfree-tool.test.sh for bounded fixture cleanup");
const helper = join(root, "bin/fm-handsfree-tool.py");
const { FmHandsfreeTool } = await import(pathToFileURL(join(root, ".opencode/plugins/fm-handsfree-tool.js")));
const originalEnv = { ...process.env };
const secret = "argument-secret-should-never-appear-in-output";
const words = "  Yes, allow this exact action.\nuser-words-private\n";
let fixtureCount = 0;

async function fixture(mode = "prompt", codeRoot = root) {
  const home = join(temporary, `hook-${++fixtureCount}`);
  await mkdir(join(home, "config"), { recursive: true, mode: 0o700 });
  const env = { ...originalEnv, FM_HOME: home, FM_ROOT_OVERRIDE: codeRoot,
    FM_CONFIG_OVERRIDE: join(home, "config"), PYTHONDONTWRITEBYTECODE: "1" };
  delete env.FM_TASK_ID;
  await writeFile(join(home, "config/handsfree-approval.json"),
    JSON.stringify({ version: 1, mode }), { mode: 0o600 });
  return { home, env };
}

function cli(f, command, data, task = "", parser = helper) {
  const env = { ...f.env };
  if (task) env.FM_TASK_ID = task;
  else delete env.FM_TASK_ID;
  const result = spawnSync("python3", [parser, command], {
    env, encoding: "utf8", input: typeof data === "string" ? data : JSON.stringify(data),
  });
  assert.ifError(result.error);
  assert.ok(!result.stdout.includes(secret) && !result.stderr.includes(secret), "CLI leaked arguments");
  assert.ok(!result.stdout.includes(words.trim()) && !result.stderr.includes(words.trim()), "CLI leaked user words");
  return { code: result.status, out: result.stdout,
    data: result.stdout.trim().startsWith("{") || result.stdout.trim().startsWith("[")
      ? JSON.parse(result.stdout) : null };
}

async function plugin(f, task = "") {
  // Factories capture their own launch identity even when multiple roles coexist.
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, f.env);
  if (task) process.env.FM_TASK_ID = task;
  try {
    return (await FmHandsfreeTool({ directory: temporary, worktree: temporary }))["tool.execute.before"];
  } finally {
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, originalEnv);
  }
}

const invocation = (sessionID = "session-hook", tool = "bash") => ({ tool, sessionID, callID: "call-1" });
const output = (command = `printf ${secret}`) => ({ args: { command, description: "action", timeout: 1000 } });
const action = (task = "worker-hook", session = "session-hook", args = output().args) =>
  ({ task_id: task, session_id: session, tool: "bash", args });

async function blocked(hook, input = invocation(), out = output(), terminalStatus = "") {
  let error;
  try { await hook(input, out); } catch (caught) { error = caught; }
  assert.ok(error instanceof Error, "before hook must throw to prevent execution");
  assert.ok(!error.message.includes(secret) && !error.message.includes(words.trim()), "hook leaked captured data");
  if (terminalStatus) {
    assert.match(error.message, new RegExp(`is ${terminalStatus}\\.`));
    assert.match(error.message, /Do not retry this action or resolve this record/);
    assert.match(error.message, /Report this terminal outcome to FirstMate and stop that action/);
    assert.doesNotMatch(error.message, /needs-decision|ask the user|resolve allow\/deny|FM_HANDSFREE_RESOLUTION/);
  } else {
    assert.match(error.message, /needs-decision.*FirstMate/s);
    if (/request [0-9a-f]{32}/.test(error.message)) {
      assert.match(error.message, /is pending/);
      assert.match(error.message, /ask the user in its own conversation, and resolve allow\/deny/);
      assert.match(error.message, /FM_HANDSFREE_RESOLUTION/);
    }
  }
  assert.doesNotMatch(error.message, /open (?:the |your |a )?worker (?:chat|window)/i);
  return error.message.match(/request ([0-9a-f]{32})/)?.[1];
}

async function record(f, id) {
  return JSON.parse(await readFile(join(f.home, "state/handsfree-tools/requests", `${id}.json`), "utf8"));
}

async function resolveViaPrimary(f, hook, id, decision = "allow", capturedWords = words) {
  const template = cli(f, "resolver-command").out.trimEnd();
  const payload = { request_id: id, decision, user_words: capturedWords };
  const command = template.replace("{payload}", JSON.stringify(payload));
  await hook(invocation("primary-session"), { args: { command, description: "Record the user's answer" } });
  // Execute the actual fixed quoted-heredoc form after the real hook accepts it.
  const result = spawnSync("bash", ["-c", command], { env: f.env, encoding: "utf8" });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr);
  assert.ok(!result.stdout.includes(secret) && !result.stdout.includes(capturedWords));
  return command;
}

{
  const f = await fixture("no_prompt");
  const hook = await plugin(f, "worker-hook");
  const out = output();
  await hook(invocation(), out);
  await hook(invocation("session-hook", "write"), { args: { filePath: "/tmp/example", content: secret } });
  assert.deepEqual(out, output(), "hook must not change the actual invocation");
  await assert.rejects(stat(join(f.home, "state")), { code: "ENOENT" });
  console.log("ok - native no_prompt hook permits actions without a question or permission API");
}

{
  const f = await fixture();
  const worker = await plugin(f, "worker-hook");
  const primary = await plugin(f);
  let executed = 0;
  try { await worker(invocation(), output()); executed++; } catch { /* native executor does not run */ }
  assert.equal(executed, 0);
  const id = await blocked(worker);
  assert.equal(await blocked(worker), id);
  const pending = cli(f, "pending", {});
  assert.equal(pending.code, 0);
  assert.equal(pending.data[0].request_id, id);
  const saved = await record(f, id);
  assert.deepEqual(saved.action, action());
  const statusText = await readFile(join(f.home, "state/worker-hook.status"), "utf8");
  assert.equal(statusText.trim().split("\n").length, 1);
  assert.match(statusText, new RegExp(`needs-decision \\[key=handsfree-${id}\\]`));
  assert.ok(!statusText.includes(secret));
  // FirstMate can read the request with a bounded native read, without a new decision.
  await primary(invocation("primary-session", "read"), {
    args: { filePath: join(f.home, pending.data[0].request_file), limit: 2000 },
  });
  assert.equal(cli(f, "resolve", { request_id: id, decision: "allow", user_words: words }, "worker-hook").code, 2);
  assert.equal(cli(f, "pending", {}, "worker-hook").code, 2);
  const command = await resolveViaPrimary(f, primary, id);
  assert.equal((await record(f, id)).resolution.user_words, words);
  assert.equal((await record(f, id)).status, "allowed");
  // Wrong bindings cannot steal or consume the grant, with or without an ID.
  for (const changed of [
    { ...action(), session_id: "new-session" },
    { ...action(), task_id: "other-worker" },
    { ...action(), tool: "write" },
    { ...action(), args: { ...output().args, command: "different" } },
    { ...action(), args: { ...output().args, timeout: 2000 } },
  ]) {
    assert.equal(cli(f, "check", { ...changed, request_id: id }, changed.task_id).code, 2);
    assert.equal(cli(f, "check", changed, changed.task_id).code, 3);
  }
  assert.equal((await record(f, id)).status, "allowed");
  await worker(invocation(), output());
  assert.equal((await record(f, id)).status, "consumed");
  assert.equal(await blocked(worker, invocation(), output(), "consumed"), id);
  assert.equal(cli(f, "resolve", { request_id: id, decision: "allow", user_words: words }).code, 4);
  assert.equal(await blocked(worker, invocation(), { args: { command } }) !== id, true);
  for (const path of ["", "requests", "actions"]) {
    assert.equal((await stat(join(f.home, "state/handsfree-tools", path))).mode & 0o777, 0o700);
  }
  for (const directory of ["requests", "actions"]) {
    for (const name of await readdir(join(f.home, "state/handsfree-tools", directory))) {
      assert.equal((await stat(join(f.home, "state/handsfree-tools", directory, name))).mode & 0o777, 0o600);
    }
  }
  console.log("ok - native hook sends FirstMate the decision; primary resolution permits one exact retry");
}

{
  const f = await fixture();
  const worker = await plugin(f, "worker-race");
  const attempts = await Promise.all(Array.from({ length: 10 }, () => blocked(worker)));
  assert.equal(new Set(attempts).size, 1, "concurrent pending actions must deduplicate atomically");
  const id = attempts[0];
  const primary = await plugin(f);
  await resolveViaPrimary(f, primary, id);
  const retries = await Promise.allSettled(Array.from({ length: 10 }, () => worker(invocation(), output())));
  assert.equal(retries.filter((result) => result.status === "fulfilled").length, 1,
    "concurrent retries must consume a grant only once");
  console.log("ok - concurrent requests deduplicate and only one concurrent retry can consume a grant");
}

{
  const f = await fixture();
  const worker = await plugin(f, "worker-deny");
  const primary = await plugin(f);
  const id = await blocked(worker);
  await resolveViaPrimary(f, primary, id, "deny", "No, do not run that action.");
  for (let i = 0; i < 2; i++) {
    assert.equal(await blocked(worker, invocation(), output(), "denied"), id);
    assert.equal(cli(f, "resolve", { request_id: id, decision: "allow", user_words: "Changed" }).code, 4);
  }
  assert.equal((await record(f, id)).status, "denied");
  const unknown = "0".repeat(32);
  assert.equal(cli(f, "resolve", { request_id: unknown, decision: "deny", user_words: "No" }).code, 2);
  assert.equal(cli(f, "check", { ...action("worker-deny"), request_id: unknown }, "worker-deny").code, 2);
  console.log("ok - denial is stable and unknown IDs cannot resolve or replay");
}

{
  // Expiry fixtures alter only their own private records; no sleeps or clock stubs.
  const f = await fixture();
  const worker = await plugin(f, "worker-expiry");
  const primary = await plugin(f);
  for (const grant of [false, true]) {
    const out = output(`expiry-${grant}`);
    const id = await blocked(worker, invocation(), out);
    if (grant) await resolveViaPrimary(f, primary, id);
    const stale = await record(f, id);
    stale.expires_at = Date.now() / 1000 - 1;
    await writeFile(join(f.home, "state/handsfree-tools/requests", `${id}.json`), JSON.stringify(stale));
    assert.equal(await blocked(worker, invocation(), out, "expired"), id);
    assert.equal((await record(f, id)).status, "expired");
    assert.equal(cli(f, "resolve", { request_id: id, decision: "allow", user_words: words }).code, 4);
  }
  console.log("ok - both pending requests and unconsumed grants reject stale retries");
}

{
  const f = await fixture();
  const primary = await plugin(f);
  const worker = await plugin(f, "worker-exemption");
  const id = await blocked(worker);
  const template = cli(f, "resolver-command").out.trimEnd();
  const payload = JSON.stringify({ request_id: id, decision: "allow", user_words: words });
  const exact = template.replace("{payload}", payload);
  await primary(invocation(), { args: { command: exact } });
  for (const command of [
    `${exact}\nprintf extra`, `printf before\n${exact}`, `echo ${exact}`,
    exact.replace("<<'FM_HANDSFREE_RESOLUTION'", "<<FM_HANDSFREE_RESOLUTION"),
    exact.replace("python3 ", "python3 -c 'print(1)' "),
    template.replace("{payload}", `${payload}\nFM_HANDSFREE_RESOLUTION\nprintf extra`),
    template.replace("{payload}", "{}"),
    "echo fm-handsfree-tool.py resolve; touch extra",
  ]) {
    assert.ok(await blocked(primary, invocation(), { args: { command } }));
  }
  assert.ok(await blocked(primary, invocation(), { args: { command: exact, workdir: temporary } }));
  assert.ok(await blocked(worker, invocation(), { args: { command: exact } }));
  // Quoted heredoc preserves shell-looking user words as data.
  const marker = join(temporary, "must-not-exist");
  await resolveViaPrimary(f, primary, id, "deny", `No. $(touch '${marker}') \`touch '${marker}'\``);
  await assert.rejects(stat(marker), { code: "ENOENT" });
  for (const [tool, args] of [
    ["read", { filePath: helper, limit: 10 }],
    ["grep", { pattern: "policy_mode", path: root }],
    ["glob", { pattern: "*.py", path: root }],
  ]) await primary(invocation("primary-session", tool), { args });
  for (const [tool, args] of [
    ["read", { filePath: helper }], ["read", { filePath: helper, limit: 100000 }],
    ["read", { filePath: helper, limit: 1, command: "extra" }],
    ["bash", { command: "cat public-file" }], ["webfetch", { url: "https://example.com" }],
    ["task", { prompt: "work" }], ["mcp_read", { filePath: helper, limit: 1 }],
  ]) assert.ok(await blocked(primary, invocation("primary-session", tool), { args }));
  assert.equal(await blocked(primary, { tool: "bash", sessionID: "session-malformed", args: {} }, {}), undefined);
  console.log("ok - exemptions are bounded native reads and one exact primary resolver; shell injection is rejected");
}

{
  const f = await fixture();
  const worker = await plugin(f, "worker-private");
  const outside = join(temporary, "outside-private");
  await mkdir(outside);
  await mkdir(join(f.home, "state"));
  await symlink(outside, join(f.home, "state/handsfree-tools"));
  assert.equal(await blocked(worker), undefined);
  assert.deepEqual(await readdir(outside), []);
  const unsafe = await fixture();
  await chmod(join(unsafe.home, "config/handsfree-approval.json"), 0o644);
  assert.equal(await blocked(await plugin(unsafe, "worker-private")), undefined);
  const malformed = await fixture();
  for (const input of [
    '{"session_id":"one","session_id":"two"}',
    '{"session_id":NaN}', JSON.stringify({ ...action(), task_id: "../escape" }),
    JSON.stringify({ ...action(), args: null }),
  ]) assert.equal(cli(malformed, "check", input, "worker-hook").code, 2);
  assert.equal(cli(malformed, "check", { ...action(), task_id: "other-worker" }, "worker-hook").code, 2);
  const missing = await fixture("prompt", join(temporary, "missing-helper"));
  assert.equal(await blocked(await plugin(missing, "worker-missing")), undefined);
  console.log("ok - unsafe metadata, invalid inputs, worker identity mismatch and missing helper all block");
}

{
  // Importlib must read policy/defaults from the helper's own code root.
  const code = join(temporary, "policy-code");
  await mkdir(join(code, "bin"), { recursive: true });
  for (const name of ["fm-handsfree-tool.py", "fm-handsfree-answer.py"]) {
    await copyFile(join(root, "bin", name), join(code, "bin", name));
  }
  const copiedHelper = join(code, "bin/fm-handsfree-tool.py");
  for (const mode of ["prompt", "no_prompt"]) {
    await writeFile(join(code, ".firstmate-defaults.json"), JSON.stringify({ approval_mode: mode }));
    const f = await fixture(mode, code);
    f.env.FM_CONFIG_OVERRIDE = join(f.home, "absent-config");
    assert.equal(cli(f, "mode", {}, "", copiedHelper).out.trim(), mode);
    const hook = await plugin(f, "worker-default");
    if (mode === "prompt") assert.ok(await blocked(hook));
    else await hook(invocation(), output());
  }
  await assert.rejects(stat(join(code, "bin/__pycache__")), { code: "ENOENT" });
  console.log("ok - shared policy import preserves both tracked defaults without bytecode artifacts");
}
