"""Run GitHub operations using only the verified quuxio CLI credential."""
import base64
import json
import os
import subprocess
import sys

env = os.environ.copy()
for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
    env.pop(key, None)
cli = '/opt/homebrew/bin/gh'
token = subprocess.check_output([cli, 'auth', 'token', '--hostname', 'github.com', '--user', 'quuxio'], env=env, text=True).strip()
assert token
env['GH_TOKEN'] = token
assert json.loads(subprocess.check_output([cli, 'api', 'user'], env=env, text=True))['login'] == 'quuxio'
args = sys.argv[1:]
if args and args[0] == 'git':
    env.update({'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': '', 'GIT_CONFIG_KEY_1': 'http.https://github.com/.extraheader', 'GIT_CONFIG_VALUE_1': 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode(), 'GIT_CONFIG_KEY_2': 'credential.interactive', 'GIT_CONFIG_VALUE_2': 'false', 'GIT_TERMINAL_PROMPT': '0'})
    command = ['/usr/bin/git', *args[1:]]
else:
    command = [cli, *args]
sys.exit(subprocess.run(command, env=env).returncode)
