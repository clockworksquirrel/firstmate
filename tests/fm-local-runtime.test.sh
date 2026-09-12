#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
TMP_ROOT=$(fm_test_tmproot fm-local-runtime)
python3 - "$ROOT" "$TMP_ROOT" <<'PY'
import json, os, pathlib, subprocess, sys
root, tmp = map(pathlib.Path, sys.argv[1:])
runtime = root / "bin/fm-local-runtime.py"
env = dict(os.environ, FM_HOME=str(tmp / "home"), FM_CONFIG_OVERRIDE=str(tmp / "home/config"))
def call(*args, good=True):
    p = subprocess.run([sys.executable, str(runtime), *args], env=env, capture_output=True, text=True)
    assert (p.returncode == 0) == good, (args, p.returncode, p.stdout, p.stderr)
    return p
status = json.loads(call("status").stdout)
assert status["automatic_fallback"] is False
assert all(x["runtime"] == "opencode" for x in status["routes"].values())
catalog = tmp / "catalog"
catalog.write_text("example/gpt-6-astra\nexample/claude-fable-5-1\n")
for slot in (1, 2, 3, 8):
    route = json.loads(call("select", "worker", "--slot", str(slot), "--catalog", str(catalog)).stdout)
    assert route["model"] == ("example/claude-fable-5-1" if slot <= 2 else "example/gpt-6-astra")
assert json.loads(call("select", "lead", "--catalog", str(catalog)).stdout)["effort"] == "xhigh"
catalog.write_text("example/gpt-5.6-sol\n")
call("select", "lead", "--catalog", str(catalog), good=False)
catalog.write_text("one/gpt-6-astra\ntwo/gpt-6-astra\n")
call("select", "lead", "--catalog", str(catalog), good=False)
call("select", "worker", "--slot", "0", good=False)
for positional in ("codex", "custom --model different"):
    conflict = subprocess.run(["bash", str(root/"bin/fm-spawn.sh"), "slot-conflict", "project", positional,
                               "--worker-slot", "1"], env=env, capture_output=True, text=True)
    assert conflict.returncode != 0 and "positional runtime" in conflict.stderr, (conflict.stdout, conflict.stderr)
assert not list((pathlib.Path(env["FM_HOME"])/"state").rglob("*"))

fakebin = tmp / "fakebin"
fakebin.mkdir()
fake = fakebin / "opencode"
fake.write_text("""#!/usr/bin/env python3
import json,os,sys
if sys.argv[1:]==["models"]:
 print("example/gpt-6-astra\\nexample/claude-fable-5-1")
elif sys.argv[1:]==["debug","config"]:
 print(os.environ["OPENCODE_CONFIG_CONTENT"])
elif sys.argv[1:]==["debug","paths"]:
 for name in ("XDG_DATA_HOME","XDG_CACHE_HOME","XDG_STATE_HOME"): print(os.environ[name])
elif sys.argv[1:]==["session","list","--format","json"]:
 print("[]")
else:
 print(json.dumps({"argv":sys.argv[1:],"config":json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]),
 "data":os.environ["XDG_DATA_HOME"],"cache":os.environ["XDG_CACHE_HOME"],
 "state":os.environ["XDG_STATE_HOME"],"config_home_present":"XDG_CONFIG_HOME" in os.environ}))
""")
fake.chmod(0o755)
env["PATH"] = str(fakebin) + os.pathsep + env["PATH"]
a = json.loads(call("launch", "--prompt", "A normal task").stdout)
b = json.loads(call("launch", "--prompt", "A second normal task").stdout)
assert a["data"] != b["data"]
assert a["data"] != a["state"] != a["cache"]
assert not a["config_home_present"]
assert a["argv"] == ["--agent","firstmate","--model","example/gpt-6-astra","--prompt","A normal task"]
assert a["config"]["provider"]["example"]["models"]["gpt-6-astra"]["options"]["reasoningEffort"]=="xhigh"
assert "permission" not in a["config"]
assert a["config"]["plugin"] == [(root/".opencode/plugins/fm-handsfree-tool.js").as_uri()]
verified = json.loads(call("launch", "--verify-only").stdout)
assert verified["action_hook_configured"] and not verified["generation_started"]
assert call("resolve", "gpt-6-astra").stdout.strip() == "example/gpt-6-astra"
call("resolve", "missing", good=False)
config = pathlib.Path(env["FM_CONFIG_OVERRIDE"])
config.mkdir(parents=True)
setup = json.loads(call("setup").stdout)
assert setup["profile"] == "principal-review" and not setup["generation_started"]
assert (config/"coordination-profile").read_text() == "principal-review\n"
call("setup")
(config/"coordination-profile").write_text("standard\n")
call("setup", good=False)
assert (config/"coordination-profile").read_text() == "standard\n"
changed=status["routes"]
changed["lead"]={"runtime":"codex","model":"gpt-6-astra","effort":"xhigh"}
(config/"runtime-policy.json").write_text(json.dumps(changed))
assert json.loads(call("select","lead").stdout)["runtime"]=="codex"
call("launch",good=False)
print("ok - local OpenCode default, exact models, isolated ordinary agents, and no subscription fallback")
PY
