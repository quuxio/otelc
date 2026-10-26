"""Before/after component checks, never creating outputs in real SDK source trees."""
import hashlib,io,json,os,subprocess,sys,types,unittest
from pathlib import Path
ROOT=Path('/Users/sclarke/github/otelc-qualification/work/rust-owned-input-binding')
OUT=Path('/Users/sclarke/github/otelc-qualification/2026-10-26/rust-owned-input-binding')
os.chdir(ROOT);sys.path.insert(0,str(ROOT))
from tests import test_rust_owned_build as tests
if sys.argv[1]=='before':
 raw=subprocess.check_output(['git','show','4250d17f2bf4d17b5c31b065ef19205897d5a209:otelc/rust_owned_build.py'])
 path=OUT/'original-owned-build.py';path.write_bytes(raw)
 module=types.ModuleType('original_owned_build');module.__file__=str(ROOT/'otelc/rust_owned_build.py');sys.modules[module.__name__]=module
 exec(compile(raw,str(path),'exec'),module.__dict__);tests.owned=module
 names=['test_nested_identity_integer_facts_cannot_be_boolean_or_float_substitutes','test_sdk_source_directory_is_not_an_owned_build_output']
 suite=unittest.TestSuite(tests.RustOwnedBuildTests(name) for name in names)
else:suite=unittest.defaultTestLoader.loadTestsFromModule(tests)
stream=io.StringIO();result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
ver=str(sys.version_info.major)+str(sys.version_info.minor)
(OUT/(sys.argv[1]+'-'+ver+'.log')).write_text(stream.getvalue())
if sys.argv[1]=='before':assert result.testsRun==2 and len(result.failures)==11 and not result.errors
else:assert result.testsRun==13 and result.wasSuccessful()
print(json.dumps({'python':ver,'mode':sys.argv[1],'methods':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)}))
