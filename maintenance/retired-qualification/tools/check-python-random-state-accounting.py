import ast,json,os,subprocess
from pathlib import Path
import yaml
ROOT=Path('/Users/sclarke/github/otelc-qualification/work/python-random-state-accounting')
OUT=Path('/Users/sclarke/github/otelc-qualification/2026-10-26/python-random-state-accounting')
modules=['test_python_random_state.RetryCaptureTests','test_python_frontend_components.PythonFrontendComponentTests.test_public_sdk_ids_attributes_sampling_and_failure_accounting','test_python_frontend_components.PythonFrontendComponentTests.test_sdk_event_and_link_attribute_drops_survive_the_process_bridge','test_python_sdk_process.PythonSdkProcessTests.test_native_fixture_four_states_rng_output_and_decoded_span_metadata']
results=[]
for version in ('312','314'):
 python='/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-'+version+'/bin/python'
 env={**os.environ,'PYTHONPATH':str(ROOT/'tests'),'PYTHONDONTWRITEBYTECODE':'1'}
 command=[python,'-B','-m','unittest',*modules,'-v']
 done=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,timeout=90)
 raw=done.stdout+done.stderr;(OUT/(version+'.log')).write_bytes(raw)
 assert done.returncode==0,raw.decode()
 assert b'Ran 5 tests' in raw and b'\nOK\n' in raw
 results.append({'python':version,'methods':5,'exit':done.returncode})
ast.parse((ROOT/'tests/test_python_random_state.py').read_text())
yaml.safe_load((ROOT/'.github/workflows/python-random-state.yml').read_text())
for command in (['/opt/homebrew/bin/markdownlint','docs/python-native.md'],['git','diff','--check']):
 subprocess.run(command,cwd=ROOT,capture_output=True,check=True)
value={'results':results,'local_native_compiler_builds':0,'actual_python_sdk_children':'existing isolated-SDK component fixtures executed','public_native_cli_rng_cases':'Not executed locally below8GiB; exact-head hosted public CLI4signal modes x2transport states required','ast_yaml_markdown_diff':'pass','scope':'Two new real HTTP harness methods and three existing SDK/ID/bridge methods perPython. Not new public-native CLI qualification or whole-Python admission.'}
(OUT/'local-checks.json').write_text(json.dumps(value,indent=2)+'\n')
print(json.dumps(value))
