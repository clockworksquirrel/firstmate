#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
TMP_ROOT=$(fm_test_tmproot fm-handsfree-global)
python3 - "$ROOT" "$TMP_ROOT" <<'PY'
import json, os, pathlib, subprocess, sys
root, tmp = map(pathlib.Path, sys.argv[1:])
helper = root / 'bin/fm-handsfree-answer.py'
tool = root / 'bin/fm-handsfree-tool.py'
global_root = tmp / 'global'
env = dict(os.environ, FM_GLOBAL_CONFIG_OVERRIDE=str(global_root), FM_HOME=str(tmp/'one'), FM_CONFIG_OVERRIDE=str(tmp/'one/config'))
env.pop('FM_TASK_ID', None)
def run(*args, good=True, selected=None):
    result = subprocess.run([sys.executable,str(helper),*args],env=selected or env,capture_output=True,text=True,timeout=3)
    assert (result.returncode == 0) == good, (args,result.returncode,result.stdout,result.stderr)
    return result
assert json.loads(run('policy').stdout)['source'] == 'repository'
run('set-global-mode','no_prompt')
policy_file = global_root/'handsfree-approval.json'
assert policy_file.stat().st_mode & 0o777 == 0o600
assert global_root.stat().st_mode & 0o777 == 0o700
for home in ('one','two','fresh'):
    selected = dict(env, FM_HOME=str(tmp/home), FM_CONFIG_OVERRIDE=str(tmp/home/'config'))
    details = json.loads(run('policy',selected=selected).stdout)
    assert details == {'mode':'no_prompt','source':'global','path':str(policy_file)}
    action = {'session_id':home,'task_id':'','tool':'bash','args':{'command':'fixture only; never executed'}}
    result = subprocess.run([sys.executable,str(tool),'check'],input=json.dumps(action),env=selected,capture_output=True,text=True,timeout=3)
    assert result.returncode == 0 and json.loads(result.stdout)['reason'] == 'no_prompt'
    assert not (tmp/home/'state/handsfree-tools').exists()
run('set-mode','prompt')
assert json.loads(run('policy').stdout)['source'] == 'instance'
assert run('mode').stdout.strip() == 'prompt'
other = dict(env, FM_HOME=str(tmp/'two'), FM_CONFIG_OVERRIDE=str(tmp/'two/config'))
assert run('mode',selected=other).stdout.strip() == 'no_prompt'
worker = dict(other,FM_TASK_ID='worker')
before = policy_file.read_bytes()
run('set-global-mode','prompt',good=False,selected=worker)
run('set-mode','prompt',good=False,selected=worker)
assert policy_file.read_bytes() == before
policy_file.unlink()
assert json.loads(run('policy',selected=other).stdout)['source'] == 'repository'
for invalid in ('{}','{"version":1,"mode":"no_prompt","mode":"prompt"}','{"version":1,"mode":true}'):
    policy_file.write_text(invalid)
    policy_file.chmod(0o600)
    run('policy',good=False,selected=other)
policy_file.unlink()
target = tmp/'target'
target.write_bytes(before)
target.chmod(0o600)
policy_file.symlink_to(target)
run('policy',good=False,selected=other)
policy_file.unlink()
os.mkfifo(policy_file,0o600)
run('policy',good=False,selected=other)
policy_file.unlink()
policy_file.write_bytes(before)
policy_file.chmod(0o644)
run('policy',good=False,selected=other)
print('ok - global mode reaches fresh homes and the action hook; explicit overrides, provenance, invalid files, and worker refusals verified')
PY
