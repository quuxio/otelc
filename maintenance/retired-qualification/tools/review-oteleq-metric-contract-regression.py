"""Independently check all native metric fixture policy contracts without builds."""
import copy,hashlib,json,subprocess,sys,types
from pathlib import Path
root=Path('/Users/sclarke/github/oteleq-qualification/work/oteleq-native-metric-error-contract')
out=Path('/Users/sclarke/github/otelc-qualification/2026-10-26/oteleq-metric-error-contract-5ff-review-02')
sys.path[:0]=[str(root/'tests'),str(root/'adapters/generation'),str(root/'examples')]
import native_metric_cases as current
import policy
old=types.ModuleType('old_cases');old.__file__=str(root/'tests/native_metric_cases.py')
source=subprocess.check_output(['git','show','9f9030747c62fcf0c1ffbab5d010b36e58b77921:tests/native_metric_cases.py'],cwd=root)
exec(compile(source,old.__file__,'exec'),old.__dict__)
rows=[];unchanged=0;refused=0
for language,modes in current.MODES.items():
 for mode in modes:
  for start in current.STARTS:
   a=old.configuration(language,mode,start,Path('/declared/sdk'));b=current.configuration(language,mode,start,Path('/declared/sdk'))
   after=policy.validate(copy.deepcopy(b));assert len(after['fixtures'])==1
   if mode=='error':
    try:policy.validate(copy.deepcopy(a))
    except ValueError as error:refused+=1;message=str(error)
    else:raise AssertionError('old incomplete error declaration accepted')
    expected=copy.deepcopy(a)
    expected['fixtures'][0]['sequence'][1]['expected_baseline']={'kind':'error','category':'exception','type':'cpp.std::runtime_error'}
    assert b==expected
   else:
    assert a==b;policy.validate(copy.deepcopy(a));unchanged+=1;message=None
   rows.append({'language':language,'mode':mode,'start':start,'before_refused':message,'after':'public policy accepted'})
assert len(rows)==28 and unchanged==24 and refused==4
value={'original_head':'9f9030747c62fcf0c1ffbab5d010b36e58b77921','reviewed_head':'5ff52e0de07aaeb7f5395e35082a7dbc05b45266','configurations':28,'unchanged_valid_configurations':unchanged,'original_error_contract_refusals':refused,'after_valid_configurations':28,'rows':rows,'scope':'Actual maintained config generator and real public policy validator only; zero SDK/application/compiler builds or launches'}
(out/'independent-policy-regression.json').write_text(json.dumps(value,indent=2)+'\n');print('28 policies valid;24 byte-equivalent structures;4 original refusals reproduced')
