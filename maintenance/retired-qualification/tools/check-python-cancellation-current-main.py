"""Validate frozen Python cancellation integration without SDK/compiler builds."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib,json,os,re,subprocess,time
root=Path('/Users/sclarke/github/otelc-qualification/work/python-cancellation-current-main')
out=Path('/Users/sclarke/github/otelc-qualification/2026-10-26/python-cancellation-current-main')
manifest=json.loads((out/'source.json').read_text())
def guard():
 for name,facts in manifest['source_facts'].items():
  p=root/name;s=p.stat();actual=[hashlib.sha256(p.read_bytes()).hexdigest(),s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns];assert actual==facts,(name,actual,facts);assert [s.st_uid,s.st_gid,s.st_nlink]==manifest['extra_facts'][name]
 assert hashlib.sha256(Path(manifest['archive']).read_bytes()).hexdigest()==manifest['archive_sha256']
 assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root)
modules=['test_python_cancellation','test_python_monitor','test_python_monitor_failures','test_python_frontend_components','test_python_lifetime_controls','test_python_meter_bridge','test_python_health']
def run(version):
 py=f'/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-{version}/bin/python';data=str(out/(version+'.coverage'));env=dict(os.environ,PYTHONPATH=str(root)+':'+str(root/'tests'),PYTHONDONTWRITEBYTECODE='1');start=time.monotonic()
 cmd=[py,'-B','-m','coverage','run','--data-file='+data,'--source=otelc.python_runtime','-m','unittest',*modules,'-v']
 result=subprocess.run(cmd,cwd=root,env=env,capture_output=True,timeout=120)
 for label,b in [('stdout',result.stdout),('stderr',result.stderr)]:
  with (out/(version+'.'+label)).open('xb') as f:f.write(b)
 assert result.returncode==0,(version,result.stderr.decode()[-2500:]);match=re.search(rb'Ran (\d+) tests',result.stderr);assert match and int(match[1])==43
 report=out/(version+'-coverage.json');subprocess.run([py,'-m','coverage','json','--data-file='+data,'-o',str(report)],cwd=root,env=env,capture_output=True,check=True)
 covered=json.loads(report.read_text())['files']['otelc/python_runtime.py']['summary'];assert covered['percent_covered']>=80
 row={'python':version,'argv':cmd,'exit_code':result.returncode,'tests':43,'seconds':time.monotonic()-start,'runtime_coverage':covered,'scope':'Actual public unchanged-source baseline/instrumented CLI signal pairs and focused runtime/component regressions; no SDK/compiler builds.'};print(version,'PASS',row['seconds'],covered['percent_covered'],flush=True);return row
guard()
with ThreadPoolExecutor(max_workers=2) as pool:checks=list(pool.map(run,('312','314')))
for command in [['/opt/homebrew/bin/markdownlint','docs/python-native.md'],['git','diff','--check']]:subprocess.run(command,cwd=root,check=True)
guard()
(out/'checks.json').write_text(json.dumps({'head':manifest['head'],'source_facts_held':len(manifest['source_facts']),'extra_facts_and_archive_held':True,'checks':checks,'markdown_and_diff':'pass','remaining':'Full required hosted checks and exact integrated-head independent review'},indent=2)+'\n')
