"""Bounded current-source checks; never invoke compiler or SDK builds."""
import ast,json,os,subprocess
from pathlib import Path
import yaml
BASE=Path('/Users/sclarke/github/otelc-qualification')
ROOT=BASE/'work/rust-module-root-identity'
OUT=BASE/'2026-10-26/rust-module-root-identity'
selection=['test_rust_modules','test_rust_sonar','test_rust_compiler.RustToolRecordTests','test_rust_compiler.RustCompilerCommandTests','test_rust_public.RustInputTests','test_rust_public.RustBoundaryTests','test_rust_owned_build','test_rust_prepared']
results=[]
for version in ('312','314'):
 python='/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-'+version+'/bin/python'
 env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(ROOT)+os.pathsep+str(ROOT/'tests')}
 command=[python,'-B','-m','coverage','run','--data-file='+str(OUT/(version+'.coverage')),'--source=otelc.rust_modules,ci.rust_sonar','-m','unittest',*selection,'-v']
 done=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,timeout=60)
 raw=done.stdout+done.stderr;(OUT/(version+'.log')).write_bytes(raw)
 assert done.returncode==0 and b'Ran 82 tests' in raw and b'\nOK\n' in raw,raw.decode()
 report=OUT/(version+'-coverage.json')
 subprocess.run([python,'-B','-m','coverage','json','--data-file='+str(OUT/(version+'.coverage')),'-o',str(report)],cwd=ROOT,env=env,capture_output=True,check=True)
 files=json.loads(report.read_bytes())['files'];assert len(files)==2 and all(v['summary']['percent_covered']>=80 for v in files.values())
 results.append({'python':version,'methods':82,'coverage':{k:v['summary'] for k,v in files.items()}})
for path in ('otelc/rust_modules.py','ci/rust_module_cases.py','tests/test_rust_modules.py'):ast.parse((ROOT/path).read_text())
steps=yaml.safe_load((ROOT/'.github/workflows/rust-lifetimes.yml').read_text())['jobs']['discovery']['steps']
assert all('run' in step or 'uses' in step for step in steps)
for step in steps:
 if 'run' in step:subprocess.run(['bash','-n'],input=step['run'].encode(),check=True)
for cmd in (['/opt/homebrew/bin/markdownlint','docs/rust-private-callers.md','docs/rust-prepared.md'],['/Users/sclarke/.rustup/toolchains/1.98.1-aarch64-apple-darwin/bin/rustfmt','--edition','2024','--check','native/rust/compiler/src/modules.rs'],['git','diff','--check']):subprocess.run(cmd,cwd=ROOT,capture_output=True,check=True)
value={'results':results,'pinned_fmt_ast_yaml_bash_markdown_diff':'pass','local_native_compiler_sdk_builds':0,'local_target_applications':0,'original_failure':'ee729 compiler build/unit/Clippy pass,3module reader errors because actualrootpath empty; originalZIP/log audit retained separately','current_real_compiler_cases':'NOT run locally; fresh exacthead hosted run and per-module80%/protected checks required','limits':'Only module mapping, no private helper injection/invocation or new public profile/provider qualification'}
(OUT/'local-checks.json').write_text(json.dumps(value,indent=2)+'\n')
print(json.dumps({'methods_each_python':82,'coverage':results[0]['coverage']}))
