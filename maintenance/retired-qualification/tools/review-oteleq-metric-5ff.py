"""Independent bounded record-test review; never compile SDKs or applications."""
from pathlib import Path
import hashlib,json,os,subprocess,time
ROOT=Path('/Users/sclarke/github/oteleq-qualification/work/oteleq-native-metric-error-contract')
OUT=Path('/Users/sclarke/github/otelc-qualification/2026-10-26/oteleq-metric-error-contract-5ff-review-02')
OUT.mkdir(parents=True,exist_ok=True)
MANIFEST=Path('/Users/sclarke/github/oteleq-qualification/2026-10-25/native-metric-controls/source-5ff52e0.json')
v=json.loads(MANIFEST.read_text())
def freeze_check():
 assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()==v['head']
 assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT)
 for name,facts in v['source_facts'].items():
  p=ROOT/name;s=p.stat();actual=[hashlib.sha256(p.read_bytes()).hexdigest(),s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
  assert actual==facts[:7],(name,actual,facts)
  if len(facts)==10:assert [s.st_uid,s.st_gid,s.st_nlink]==facts[7:]
  assert [s.st_uid,s.st_gid,s.st_nlink]==v['extra_facts'][name]
 assert hashlib.sha256(Path(v['archive']).read_bytes()).hexdigest()==v['archive_sha256']
 return {'head':v['head'],'files':len(v['source_facts']),'archive_sha256':v['archive_sha256'],'all_source_and_extra_facts_held':True}
def keep(name,data):
 p=OUT/name
 if p.exists():assert p.read_bytes()==data
 else:
  with p.open('xb') as f:f.write(data)
checks=[]
def run(name,cmd,expected=0):
 started=time.monotonic();env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(ROOT/'tests'),NODE_PATH='/Users/sclarke/github/oteleq/ci/markdownlint/node_modules',CC='/opt/homebrew/opt/llvm@22/bin/clang',CXX='/opt/homebrew/opt/llvm@22/bin/clang++',LLVM_CONFIG='/opt/homebrew/opt/llvm@22/bin/llvm-config',OTELC_LLVM_BIN='/opt/homebrew/opt/llvm@22/bin',OTELC_CLANG='/opt/homebrew/opt/llvm@22/bin/clang',OTELC_CLANGXX='/opt/homebrew/opt/llvm@22/bin/clang++')
 r=subprocess.run(cmd,cwd=ROOT,env=env,capture_output=True,timeout=120)
 keep(name+'.stdout',r.stdout);keep(name+'.stderr',r.stderr)
 checks.append({'name':name,'argv':cmd,'exit_code':r.returncode,'seconds':time.monotonic()-started})
 assert r.returncode==expected,(name,r.stderr.decode()[-2500:])
 print(name,'PASS',flush=True)
 return r
before=freeze_check()
public=['test_generation_native_lifetime_policy','test_generation_native_lifetime_frontend','test_generation_native_lifetime_run','test_generation_native_source','test_generation_native_source_run']
workflow=['test_generation_native_lifetime_workflow','test_generation_native_lifetime_promotion','test_generation_native_lifetime_boundaries','test_generation_native_lifetime_errors','test_generation_native_lifetime_audit','test_generation_native_receipt_qualification']
public_include='*/native_lifetime_policy.py,*/native_lifetime_frontend.py,*/native_source.py,*/native_source_run.py'
workflow_include='*/qualify_native_lifetimes.py,*/native_lifetime_cases.py,*/native_lifetime_audit.py,*/native_lifetime_otlp.py,*/native_lifetime_refusals.py,*/native_prepared_otlp.py,*/native_lifetime_test_capture.py'
metric=['test_generation_native_metric_qualification','test_generation_native_metric_workflow','test_generation_native_metric_histogram','test_generation_native_receipt_qualification']
control=['test_generation_native_controls','test_generation_native_rendezvous','test_generation_native_sequence','test_generation_native_live','test_generation_native_control_receipt','test_generation_native_session','test_generation_native_source','test_generation_native_source_run']
metric_include='*/native_metric_cases.py,*/native_metric_frames.py,*/native_metric_health.py,*/native_metric_otlp.py,*/native_metric_histogram.py,*/native_metric_receipt.py,*/native_metric_test_capture.py,*/native_metric_refusals.py,*/qualify_native_metrics.py'
control_include='*/native_control.py,*/native_rendezvous.py,*/native_sequence.py,*/native_live.py,*/native_control_receipt.py'
for version in ('312','314'):
 py=f'/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-{version}/bin/python'
 for group,modules,include in [('metric',metric,metric_include),('control',control,control_include),('public',public,public_include),('workflow',workflow,workflow_include)]:
  data=str(OUT/(version+'-'+group+'.coverage'))
  run(version+'-'+group,[py,'-B','-m','coverage','run','--data-file='+data,'--include='+include,'-m','unittest',*modules,'-v'])
  components=include.split(',')
  for component in components:
   run(version+'-'+component[2:],[py,'-B','-m','coverage','report','--data-file='+data,'--include='+component,'--fail-under=80'])
run('workflow-node',['/opt/homebrew/bin/node','--test','tests/workflow_syntax.test.mjs'])
run('markdown',['/opt/homebrew/bin/markdownlint','docs/native-lifetime-workflows.md','docs/native-metric-controls.md','docs/native-metric-workflows.md'])
run('diff',['git','diff','--check',v['base']+'..'+v['head']])
after=freeze_check()
keep('checks.json',(json.dumps({'before':before,'after':after,'checks':checks,'scope':'Record/component tests and source review only; no SDK, compiler or application execution.'},indent=2)+'\n').encode())
print('IMMUTABLE SOURCE FACTS HELD',len(v['source_facts']),flush=True)
