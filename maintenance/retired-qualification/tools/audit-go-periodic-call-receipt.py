import concurrent.futures,gzip,hashlib,json,re,subprocess,zipfile,sys
sys.path.insert(0,"/Users/sclarke/github/otelc-qualification/work/go-periodic-call-receipt")
from otelc import go_context_bundle as context,go_telemetry_bundle as telemetry
from pathlib import Path
HEAD='b18acd57f3512ca1c31fa346b80f8f00d53120f5';RUN=40247207196
OUT=Path('/Users/sclarke/github/otelc-qualification/2026-10-26/go-periodic-call-receipt/hosted');OUT.mkdir(exist_ok=True)
CMD=['/usr/bin/python3','/Users/sclarke/github/otelc-qualification/tools/quux-remote.py','api']
def api(path):return subprocess.check_output(CMD+['repos/quuxio/otelc/'+path]+(['--allow-escape-sequences'] if path.endswith('/logs') else []))
def keep(p,b):
 if p.exists():assert p.read_bytes()==b
 else:
  with p.open('xb') as f:f.write(b)
def audit(row):
 host,artifact,job=row;meta=json.loads(api(f'actions/artifacts/{artifact}'));assert meta['workflow_run']['head_sha']==HEAD
 p=OUT/(host+'-original.zip');raw=p.read_bytes() if p.exists() else api(f'actions/artifacts/{artifact}/zip');assert len(raw)==meta['size_in_bytes'] and meta['digest']=='sha256:'+hashlib.sha256(raw).hexdigest();keep(p,raw)
 state=json.loads(api(f'actions/jobs/{job}'));assert state['conclusion']=='success' and state['run_id']==RUN
 log=api(f'actions/jobs/{job}/logs');keep(OUT/(host+'-original.log.gz'),gzip.compress(log,mtime=0))
 assert b'all modules verified' in log
 with zipfile.ZipFile(p) as z:
  assert z.read('go-foundation/head.txt').decode().strip()==HEAD
  text=z.read('go-foundation/go-tests.log').decode();packages=re.findall(r'^ok\s+(io.quux.otelc/go/\S+)\s',text,re.M);assert len(packages)==4 and len(set(packages))==4,packages
  assert re.search(r'go test[^\n]* -race ', text) and re.search(r'go .*vet ', text)
  coverage=z.read('go-coverage.out').decode().splitlines();assert coverage[0]=='mode: atomic';files={}
  for line in coverage[1:]:
   location,count,executed=line.split();source=location.rsplit(':',1)[0];total,hit=files.get(source,(0,0));count=int(count);files[source]=(total+count,hit+(count if int(executed)>0 else 0))
  assert len(files)==16 and all(100*hit/total>=80 for total,hit in files.values())
  py=json.loads(z.read('go-foundation/python-coverage.json'))['files'];assert len(py)==4 and all(v['summary']['percent_covered']>=80 for v in py.values())
  pt=z.read('go-foundation/python-tests.log').decode();tests=re.search(r'Ran (\d+) tests in',pt);assert tests and re.search(r'\nOK\n',pt)
  fixture_log=z.read('go-foundation/context-fixtures.log').decode();assert 'Ran 2 tests in' in fixture_log and re.search(r'\nOK\n',fixture_log) and 'skipped' not in fixture_log
  detached=json.loads(z.read('go-detach-component-coverage.json'));telemetry_report=json.loads(z.read('go-telemetry-component-coverage.json'));tools=json.loads(z.read('go-upstream/toolchain.json'))
  assert detached['generated_source_sha256']==hashlib.sha256(context.DETACH_SOURCE).hexdigest()
  assert detached['statement_percent']>=80 and detached['covered_statements']<=detached['total_statements']
  assert telemetry_report['generated_source_sha256']==hashlib.sha256(telemetry.SOURCE).hexdigest()
  assert len(telemetry_report['components'])==3 and all(r['statement_percent']>=80 and r['covered_statements']<=r['total_statements'] for r in telemetry_report['components'].values())
  assert tools['go_version']=='go1.27.3' and tools['module']==context.UPSTREAM_MODULE and tools['version']==context.UPSTREAM_VERSION
  assert tools['module_sum']==context.MODULE_SUM and tools['module_go_sum']==context.MODULE_GO_SUM and tools['bundle_sha256']==context.BUNDLE_SHA256

  def counts(name):
   lines=z.read(name).decode().splitlines();assert lines[0] in ('mode: atomic','mode: set','mode: count')
   blocks={}
   for line in lines[1:]:
    position,size,hits=line.split();size=int(size);hits=int(hits);assert size>=0 and hits>=0
    if position in blocks:assert blocks[position][0]==size
    blocks[position]=(size,hits+blocks.get(position,(0,0))[1])
   values={}
   for position,(size,hits) in blocks.items():
    source=position.rsplit(':',1)[0];total,covered=values.get(source,(0,0));values[source]=(total+size,covered+(size if hits else 0))
   return {n:{'covered_statements':c,'total_statements':t,'statement_percent':100*c/t} for n,(t,c) in values.items() if t}
  dr=counts('go-detach-component-coverage.out')['example.com/otelc-detach-fixture/helper.go']
  assert all(detached[k]==v for k,v in dr.items()) and dr['statement_percent']>=80
  tr=counts('go-telemetry-component-coverage.out')
  for name,value in telemetry_report['components'].items():assert tr[name]==value and value['statement_percent']>=80
  detached_source=context.DETACH_SOURCE.removeprefix(b'//go:build ignore\n').replace(b'"runtime"',b'"example.com/otelc-detach-fixture/gls"').replace(b'go.opentelemetry.io/otelc/pkg/runtime.RegisterDetachSpanFunc',b'example.com/otelc-detach-fixture/bridge.RegisterDetachSpanFunc')
  module='go.opentelemetry.io/otel/otelc-telemetry-fixture'
  telemetry_source=telemetry.SOURCE.removeprefix(b'//go:build ignore\n').replace(b'"runtime"',('runtime "'+module+'/gls"').encode()).replace(b'go.opentelemetry.io/otelc/pkg/runtime.RegisterImplicitTelemetry',(module+'/bridge.RegisterImplicitTelemetry').encode())
  assert detached['compiled_source_sha256']==hashlib.sha256(detached_source).hexdigest()
  assert telemetry_report['compiled_source_sha256']==hashlib.sha256(telemetry_source).hexdigest()
  http=counts('go-http-coverage.out');http_summary=json.loads(z.read('go-http-check.json'))['component_statement_percent']
  expected={q.name for q in Path('/Users/sclarke/github/otelc-qualification/work/go-periodic-call-receipt/adapters/go-http').glob('*.go') if not q.name.endswith('_test.go') and q.name!='http_bridge.go'}
  assert {n.removeprefix('io.quux.otelc/go-http/') for n in http}==expected
  assert {n.removeprefix('io.quux.otelc/go-http/'):v['statement_percent'] for n,v in http.items()}==http_summary
  source_coverage=counts('go-source-coverage.out');assert source_coverage and all(v['statement_percent']>=80 for v in source_coverage.values())
  assert all(v['statement_percent']>=80 for v in http.values())

 record={'head':HEAD,'run':RUN,'job':job,'artifact':meta,'artifact_sha256':hashlib.sha256(raw).hexdigest(),'original_log_sha256':hashlib.sha256(log).hexdigest(),'race_and_vet_packages':packages,'per_file_coverage':{n:100*c/t for n,(t,c) in files.items()},'python_methods':int(tests[1]),'python_component_coverage':{n:v['summary']['percent_covered'] for n,v in py.items()},'fixture_dependency_verification':'both fixture module checksum verification steps succeeded; logs retained','scope':'Exact-head SDK/race/vet and component coverage only; full CI/Sonar, independent review, public Go language qualification remain separate'}
 record.update(raw_profile_audit={'detach':dr,'telemetry':{n:tr[n] for n in telemetry_report['components']},'http':http,'source':source_coverage})
 record.update(generated_helpers={'actual_methods':2,'detached':detached,'telemetry':telemetry_report,'tools':tools,'limits':'Exact generated algorithms with fixture TLS/link endpoints and real official SDK; not native TLS/hooks/tasks or whole-language qualification'})
 keep(OUT/(host+'-audit.json'),(json.dumps(record,indent=2)+'\n').encode());print(host,'PASS',min(record['per_file_coverage'].values()),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as e:list(e.map(audit,[('linux',12619441097,121332490261)]))
