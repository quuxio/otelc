"""Independent exact-source review checks for Rust source and owned metadata readers."""
from __future__ import annotations
import ast,datetime,hashlib,json,os,re,subprocess
from pathlib import Path
BASE=Path('/Users/sclarke/github/otelc-qualification')
SDK=BASE/'work/rust-prepared-constructor-section'
TARGETS=[('source','da16b71f9c27829aa9df94b9d3327088f604db9c','oteleq-rust-source-build','rust-source-build-source',56,681),('owned','da94baf4ae460bc4a4b197924e0fa0b97ca57602','oteleq-rust-owned-provider','rust-owned-source',64,683)]

def sha(raw):return hashlib.sha256(raw).hexdigest()
def hold(manifest):
    for name,facts in manifest['source_facts'].items():
        path=Path(manifest['root'])/name;s=path.stat()
        assert [sha(path.read_bytes()),s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns]==facts,name
        assert [s.st_uid,s.st_gid,s.st_nlink]==manifest['extra_facts'][name],name
    assert sha(Path(manifest['archive']).read_bytes())==manifest['archive_sha256']
    return len(manifest['source_facts'])
def literal(path,name):
    tree=ast.parse(path.read_bytes())
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id==name for target in node.targets))
for role,head,folder,source_name,count,facts in TARGETS:
    root=Path('/Users/sclarke/github/oteleq-qualification/work')/folder
    manifest=json.loads((Path('/Users/sclarke/github/oteleq-qualification/2026-10-26')/source_name/'source.json').read_bytes())
    out=BASE/('2026-10-26/oteleq-rust-'+role+'-'+head[:7]+'-review');out.mkdir(mode=0o700)
    assert manifest['head']==head and Path(manifest['root'])==root and hold(manifest)==facts
    assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root)
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()==head
    assert literal(root/'adapters/generation/native_build.py','BOOTSTRAP')==literal(SDK/'otelc/native_launch.py','BOOTSTRAP')
    modules=['rust_tools','rust_protocol','rust_inventory','rust_lifetimes','rust_build_recipe','rust_source_build']
    tests=['test_generation_rust_tools','test_generation_rust_protocol','test_generation_rust_inventory','test_generation_rust_lifetimes','test_generation_rust_source_build']
    if role=='owned':modules.append('rust_owned_build');tests.append('test_generation_rust_owned_build')
    tests+=['test_generation_native_prepared_inputs','test_generation_native_prepared_build.NativeToolchainAndSDKTests']
    embedded=(root/'src/generation.rs').read_text();identity=(root/'adapters/generation/generation.py').read_text()
    for module in modules:
        assert 'include_str!("../adapters/generation/'+module+'.py")' in embedded
        assert "'"+module+".py'" in identity
    results=[]
    for version in ('312','314'):
        py=Path('/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-'+version+'/bin/python')
        env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(root/'tests')}
        cov=out/(version+'.coverage');command=[str(py),'-B','-m','coverage','run','--data-file='+str(cov),'--include='+','.join('*/'+m+'.py' for m in modules),'-m','unittest',*tests,'-v']
        run=subprocess.run(command,cwd=root,env=env,capture_output=True,timeout=60);raw=run.stdout+run.stderr;(out/(version+'.log')).write_bytes(raw)
        assert run.returncode==0,raw.decode();assert int(re.search(rb'Ran (\d+) tests',raw).group(1))==count
        path=out/(version+'-coverage.json');subprocess.run([str(py),'-B','-m','coverage','json','--data-file='+str(cov),'-o',str(path)],cwd=root,env=env,capture_output=True,check=True)
        rows=json.loads(path.read_bytes())['files'];assert len(rows)==len(modules) and all(row['summary']['percent_covered']==100 for row in rows.values())
        results.append({'python':version,'methods':count,'coverage':{name:row['summary'] for name,row in rows.items()}})
    for command in (['/opt/homebrew/bin/markdownlint','README.md','docs/completion.md','docs/rust-prepared-provider.md'],['git','diff','--check']):subprocess.run(command,cwd=root,capture_output=True,check=True)
    assert hold(manifest)==facts
    review=['Caller-held canonical source/dependency files and independent pre-build function/MIR records precede SDK application output. Immutable policy JSON and source/SDK/tool snapshots prevent rehashed producer requests/inventories from replacing caller intent.',
      'Fixed discovery/inspect/lifetime/plain/bootstrap/weave commands, cwd, product paths and request denominators match actual SDK rust_build/rust_compiler contracts read separately; exact bootstrap bytes match by AST extraction without SDK import or compiler execution.',
      'Required channel bytes and closed compiler results retain exact primitive identities, positive PIDs/zero exits/finite1200s bounds. Full UTF-8 MIR fingerprints, complete declared original source denominator, compiled selectors and physical output boundaries are checked.',
      'Original File/Record facts refuse changes and restored bytes; returned dictionaries are fresh. New assets are Rust-embedded and bound to generation identity; per-module floors are in focused/full checks. No runtime backend is enabled.']
    if role=='owned':review.append('Owned envelope and closed6-file output bind the accepted original source manifest, caller-held fixed gate source and resolved LLVM C compiler before gate build. Fixed C11 argv/compiler database cwd and full nested primitive identities match SDKnative_gate/owned05; no foreign manifest read or record repair refreshes held facts.')
    value={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'status':'clear for exact '+role+' metadata reader component and its embedding/identity wiring','head':head,'source_facts_held_before_and_after':facts,'archive_sha256':manifest['archive_sha256'],'results':results,'manual_review':review,'findings':[],
      'limits':'Caller-supplied complete policy is sealed; this is not a replacement common policy resolver. Independent compiler/dependency discovery, historical process/environment/OS provenance, complete sysroot/dependency closure, prepared runtime/health/OTLP/generated-retained/shipping admission and required hosted gates remain separate. No actual Rust distribution build performed locally.',
      'local_sdk_builds':0,'local_compiler_builds':0,'local_application_starts':0,'controller_sha256':sha(Path(__file__).read_bytes())}
    (out/'review.json').write_text(json.dumps(value,indent=2)+'\n');print(json.dumps({'role':role,'head':head,'methods_each_python':count,'facts':facts,'review_sha256':sha((out/'review.json').read_bytes())}))
