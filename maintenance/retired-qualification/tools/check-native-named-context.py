"""Bounded checks for Rust raw capture and declared receipt controls."""
from __future__ import annotations
import ast,hashlib,json,os,re,subprocess
from pathlib import Path
import yaml
BASE=Path('/Users/sclarke/github/otelc-qualification')
ROOT=BASE/'work/native-named-owned-context'
OUT=BASE/'2026-10-26/native-named-owned-context'
OUT.mkdir(mode=0o700,exist_ok=True)
results=[]
for version in ('312','314'):
    python=Path('/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-'+version+'/bin/python')
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(ROOT/'tests')}
    command=[str(python),'-B','-m','coverage','run','--data-file='+str(OUT/(version+'.coverage')),
        '--source=ci.rust_telemetry,ci.histogram_observation,otelc.prepared_rust,otelc.rust_supervisor','-m','unittest',
        'test_native_sonar','test_rust_capture_records','test_rust_telemetry','test_rust_histogram_observation','test_rust_prepared','test_rust_owned_build',
        'test_rust_public.RustInputTests','test_rust_public.RustBoundaryTests','-v']
    completed=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,timeout=60)
    raw=completed.stdout+completed.stderr
    (OUT/(version+'.log')).write_bytes(raw)
    assert completed.returncode==0,raw.decode()
    methods=int(re.search(rb'Ran (\d+) tests',raw).group(1))
    assert methods==83,methods
    path=OUT/(version+'-coverage.json')
    subprocess.run([str(python),'-B','-m','coverage','json','--data-file='+str(OUT/(version+'.coverage')),'-o',str(path)],cwd=ROOT,env=env,capture_output=True,check=True)
    files=json.loads(path.read_bytes())['files']
    assert len(files)==4 and all(row['summary']['percent_covered']>=80 for row in files.values())
    results.append({'python':version,'methods':methods,'exit':completed.returncode,'coverage':{name:row['summary'] for name,row in files.items()}})
for name in ('ci/histogram_observation.py','tests/test_rust_histogram_observation.py','ci/rust_telemetry.py','ci/rust_prepared_cases.py','tests/test_rust_telemetry.py','tests/test_rust_capture_records.py','tests/test_rust_public.py','tests/test_java_integration.py'):
    ast.parse((ROOT/name).read_text())
yaml.safe_load((ROOT/'.github/workflows/rust-runtime.yml').read_text())
for command in (['/opt/homebrew/bin/markdownlint','docs/rust-prepared.md'],['git','diff','--check']):
    subprocess.run(command,cwd=ROOT,capture_output=True,check=True)
# The shared opt-in recorder is exactly the independently reviewed native donor.
donor=subprocess.check_output(['git','-C','/Users/sclarke/github/otelc','show','9c7e4ada9b4646626291d37118ca5fbb935abc9d:tests/test_java_integration.py'])
assert (ROOT/'tests/test_java_integration.py').read_bytes()==donor
model=ROOT/'ci/histogram_observation.py'
original=Path('/Users/sclarke/github/oteleq-qualification/work/oteleq-rust-owned-provider/adapters/generation/histogram_observation.py')
assert model.read_bytes()==original.read_bytes()
value={'histogram_donor':'da94baf4ae460bc4a4b197924e0fa0b97ca57602','histogram_sha256':hashlib.sha256(model.read_bytes()).hexdigest(),'results':results,'new_receipt_methods':21,'capture_donor':'9c7e4ada9b4646626291d37118ca5fbb935abc9d','capture_source_sha256':hashlib.sha256(donor).hexdigest(),
       'ast_yaml_markdown_diff':'pass','local_sdk_builds':0,'local_compiler_builds':0,'local_application_starts':0,
       'limits':'Actual local loopback HTTP receiver tests and literal protobuf/SDK-health controls. No genuine newhead Rust/compiler/SDK execution; hosted raw-wire fixture results and independent review remain required.'}
(OUT/'local-checks.json').write_text(json.dumps(value,indent=2)+'\n')
print(json.dumps({'methods_each_python':83,'new_methods':21,'coverage':results[0]['coverage']}))
