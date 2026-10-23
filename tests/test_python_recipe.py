"""Native argv and resolved settings must follow independently retained intent."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci import build_python_inputs
from otelc.config import Policy
from otelc import python_input_journal as journal
from otelc import python_recipe as recipe


class PythonRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name).resolve()
        self.root = self.cwd / 'app'
        self.root.mkdir()
        self.runtime = self.cwd / 'runtime'
        self.runtime.mkdir(mode=0o700)
        (self.root / 'entry.py').write_text('print("original")\n')
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['python'],
            'metrics': {'collection': False, 'emission': False},
            'functions': {'include': ['entry.*']}, 'context': {'tasks': True}})

    def requested(self, **changes) -> dict:
        options = {'root': 'app', 'entry': './entry.py', 'module': None,
                   'arguments': ('', '--switch', 'caf\u00e9'), 'plain': False}
        options.update(changes)
        with patch.object(Path, 'cwd', return_value=self.cwd):
            return recipe.request(self.policy, 'runtime', **options)

    def test_original_request_and_resolved_plan_keep_full_policy_and_path_bases(self) -> None:
        original = self.requested()
        plan = recipe.resolve_plan(original)
        self.assertEqual(original['paths']['entry'],
            {'requested': './entry.py', 'resolved': str(self.root / 'entry.py')})
        self.assertEqual(original['paths']['root']['requested'], 'app')
        self.assertEqual(original['paths']['runtime']['requested'], 'runtime')
        self.assertEqual(plan['cwd'], str(self.root))
        self.assertEqual(plan['runtime'], str(self.runtime))
        self.assertEqual(plan['native_arguments'], ['./entry.py', '', '--switch', 'caf\u00e9'])
        self.assertFalse(plan['source_complete'])
        self.assertEqual(plan['input_scope'], 'native-source-entry-only')
        self.assertEqual(plan['policy'], self.policy.data)
        recipe.verify(original, plan)
        plan['policy']['metrics']['collection'] = True
        self.assertFalse(original['policy']['metrics']['collection'])
        original['policy']['context']['tasks'] = False
        self.assertTrue(self.policy.data['context']['tasks'])

    def test_environment_is_allowlisted_and_distinguishes_absent_from_empty(self) -> None:
        with patch.dict(os.environ, {'PYTHONPATH': '', 'GH_TOKEN': 'not-recorded'}, clear=True):
            original = self.requested()
            self.assertEqual(original['environment']['PYTHONPATH'], '')
            self.assertIsNone(original['environment']['PYTHONHOME'])
            self.assertNotIn('GH_TOKEN', original['environment'])
            plan = recipe.resolve_plan(original)
            recipe.verify(original, plan, environment={'PYTHONPATH': ''})
            with self.assertRaisesRegex(ValueError, 'environment changed'):
                recipe.verify(original, plan, environment={})

    def test_fixed_interpreter_probe_does_not_run_application_site_hooks(self) -> None:
        marker = self.cwd / 'must-not-exist'
        hook = self.root / 'sitecustomize.py'
        hook.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        with patch.dict(os.environ, {'PYTHONPATH': str(self.root)}):
            original = self.requested()
            recipe.verify(original, recipe.resolve_plan(original))
        self.assertFalse(marker.exists())

    def test_policy_source_rechecks_actual_bytes_and_resolution(self) -> None:
        filename = self.cwd / 'otelc.toml'
        original_text = 'schema_version=3\nlanguages=["python"]\n'
        filename.write_text(original_text)
        self.policy = Policy.load(filename)
        original = self.requested()
        self.assertEqual(original['policy_source']['text'], original_text)
        plan = recipe.resolve_plan(original)
        recipe.verify(original, plan)
        filename.write_text(original_text + '# changed\n')
        with self.assertRaisesRegex(ValueError, 'policy source changed'):
            recipe.verify(original, plan)
        self.assertEqual(original['policy_source']['text'], original_text)

    def test_policy_source_metadata_and_parsed_policy_must_both_match(self) -> None:
        filename = self.cwd / 'otelc.toml'
        filename.write_text('schema_version=3\nlanguages=["python"]\n')
        self.policy = Policy.load(filename)
        original = self.requested()
        for change in ({'text': 'different'}, {'sha256': '0' * 64}):
            altered = copy.deepcopy(original)
            altered['policy_source'].update(change)
            plan = recipe.resolve_plan(altered)
            with self.assertRaisesRegex(ValueError, 'policy source changed'):
                recipe.verify(altered, plan)
        altered = copy.deepcopy(original)
        altered['policy']['metrics']['collection'] = False
        plan = recipe.resolve_plan(altered)
        with self.assertRaisesRegex(ValueError, 'policy source changed'):
            recipe.verify(altered, plan)

    def test_extra_recorded_command_flags_or_partial_common_policy_are_refused(self) -> None:
        original = self.requested()
        changes = [dict(command=['untrusted']), dict(schema_version=True),
            dict(profile='other'), dict(flags=['-c', 'print("other")']),
            dict(arguments=('not', 'a', 'list')), dict(arguments=[None]),
            dict(arguments=['bad\0argument']), dict(plain=1), dict(policy=None),
            dict(policy={'schema_version': 3, 'languages': ['python']}),
            dict(policy=Policy.parse({'schema_version': 3, 'languages': ['java']}).data),
            dict(environment={}), dict(environment={**original['environment'], 'GH_TOKEN': 'extra'}),
            dict(environment={**original['environment'], 'PYTHONPATH': 1})]
        for change in changes:
            with self.subTest(change=tuple(change)):
                with self.assertRaises(ValueError):
                    recipe.resolve_plan({**original, **change})
        with self.assertRaises(ValueError):
            recipe.resolve_plan(None)

    def test_entry_and_module_shapes_cannot_become_interpreter_options(self) -> None:
        original = self.requested()
        for change in (dict(module='entry'), dict(module='-c'), dict(module='bad..name'),
                       dict(module=7), dict(cwd='relative'), dict(paths={}),
                       dict(paths={'root': 'wrong', 'runtime': {}})):
            with self.subTest(change=tuple(change)):
                with self.assertRaises(ValueError):
                    recipe.resolve_plan({**original, **change})
        for change in ({'resolved': str(self.cwd / 'outside.py')},
                       {'resolved': str(self.root / 'entry.pyc')}, {'resolved': 'relative'},
                       {'requested': 'bad\0path'}, {'extra': True}):
            altered = copy.deepcopy(original)
            altered['paths']['entry'].update(change)
            with self.assertRaises(ValueError):
                recipe.resolve_plan(altered)
        with self.assertRaises(ValueError):
            recipe.resolve_plan({**original, 'module': None,
                'paths': {key: value for key, value in original['paths'].items() if key != 'entry'}})

    def test_dash_prefixed_files_keep_intent_and_cannot_become_interpreter_options(self) -> None:
        for name in ('-cprint(123).py', '-m.py', '--.py', '--help.py'):
            with self.subTest(name=name):
                (self.root / name).write_text('print("original")\n')
                original = self.requested(entry=name, arguments=('-c', '-m', '--', 'literal'))
                self.assertEqual(original['paths']['entry']['requested'], name)
                plan = recipe.resolve_plan(original)
                self.assertEqual(plan['native_arguments'], ['./' + name, '-c', '-m', '--', 'literal'])
                recipe.verify(original, plan)
                altered = copy.deepcopy(plan)
                altered['native_arguments'][0] = name
                with self.assertRaisesRegex(ValueError, 'fixed recipe'):
                    recipe.verify(original, altered)
        changed = self.requested()
        changed['paths']['entry']['requested'] = '-m.py'
        with self.assertRaisesRegex(ValueError, 'path resolution changed'):
            recipe.verify(changed, recipe.resolve_plan(changed))

    def test_interpreter_metadata_is_strict_and_rechecked_without_editing_the_binary(self) -> None:
        original = self.requested()
        for value in (None, {}, {**original['interpreter'], 'version': None}):
            with self.assertRaises(ValueError):
                recipe.resolve_plan({**original, 'interpreter': value})
        for change in ({'path': 'relative'}, {'resolved_path': 'relative'}, {'sha256': 'bad'},
                       {'size': True}, {'symlink': ''}, {'other': True}):
            altered = copy.deepcopy(original)
            altered['interpreter']['file'].update(change)
            with self.assertRaises(ValueError):
                recipe.resolve_plan(altered)
        for key in ('version', 'cache_tag', 'argv0'):
            altered = copy.deepcopy(original)
            altered['interpreter'][key] = 'changed'
            plan = recipe.resolve_plan(altered)
            with self.assertRaisesRegex(ValueError, 'interpreter metadata'):
                recipe.verify(altered, plan)
        altered = copy.deepcopy(original)
        altered['interpreter']['file']['sha256'] = '0' * 64
        plan = recipe.resolve_plan(altered)
        with self.assertRaisesRegex(ValueError, 'interpreter file changed'):
            recipe.verify(altered, plan)

    def test_entry_symlink_retarget_and_changed_caller_cwd_resolution_are_refused(self) -> None:
        link = self.root / 'linked.py'
        link.symlink_to('entry.py')
        original = self.requested(entry=Path('linked.py'))
        plan = recipe.resolve_plan(original)
        recipe.verify(original, plan)
        (self.root / 'other.py').write_text('print("other")\n')
        link.unlink()
        link.symlink_to('other.py')
        with self.assertRaisesRegex(ValueError, 'path resolution changed'):
            recipe.verify(original, plan)
        altered = copy.deepcopy(original)
        alias = self.cwd / 'alias'
        alias.symlink_to(self.cwd, target_is_directory=True)
        altered['cwd'] = str(alias)
        altered_plan = recipe.resolve_plan(altered)
        with self.assertRaisesRegex(ValueError, 'cwd resolution changed'):
            recipe.verify(altered, altered_plan)

    def test_root_runtime_and_entry_kinds_are_rechecked(self) -> None:
        original = self.requested()
        plan = recipe.resolve_plan(original)
        self.runtime.rmdir()
        self.runtime.write_text('not a directory')
        with self.assertRaisesRegex(ValueError, 'must be directories'):
            recipe.verify(original, plan)
        self.runtime.unlink()
        self.runtime.mkdir()
        entry = self.root / 'entry.py'
        entry.unlink()
        entry.mkdir()
        with self.assertRaisesRegex(ValueError, 'must be a source file'):
            recipe.verify(original, plan)

    def test_plan_edits_and_malformed_policy_source_are_refused(self) -> None:
        original = self.requested()
        for change in ({'native_arguments': ['-c', 'bad']}, {'source_complete': True},
                       {'python': '/different/python'}, {'plain': True}):
            altered_plan = {**recipe.resolve_plan(original), **change}
            with self.assertRaisesRegex(ValueError, 'fixed recipe'):
                recipe.verify(original, altered_plan)
        for source in ('wrong', {'path': None}, {'path': 'path', 'text': None}):
            altered = {**original, 'policy_source': source}
            plan = recipe.resolve_plan(altered)
            with self.assertRaisesRegex(ValueError, 'policy source'):
                recipe.verify(altered, plan)

    def test_native_command_uses_only_fixed_driver_slots_and_original_arguments(self) -> None:
        original = self.requested()
        driver = self.cwd / 'independently-selected-driver'
        self.assertEqual(recipe.native_command(original, driver, 7),
            [str(driver), '7', sys.executable, original['interpreter']['argv0'],
             './entry.py', '', '--switch', 'caf\u00e9'])
        for descriptor in (True, -1, 2 ** 31, '7'):
            with self.assertRaisesRegex(ValueError, 'descriptor'):
                recipe.native_command(original, driver, descriptor)
        relative_driver = Path('relative-driver')
        with self.assertRaisesRegex(ValueError, 'absolute path'):
            recipe.native_command(original, relative_driver, 7)
        self.assertEqual(recipe.encoded(original), recipe.encoded(copy.deepcopy(original)))

    def test_actual_native_script_and_module_keep_original_frames_metadata_and_raw_channels(self) -> None:
        driver = build_python_inputs.build()
        source = '''import json,sys
sys.stdout.buffer.write(bytes([0,255,10]))
sys.stderr.buffer.write(bytes([1,255,10]))
print(json.dumps({'argv':sys.argv,'orig_argv':sys.orig_argv,
'file':__file__,'name':__name__,'back':sys._getframe().f_back is None,
'spec':None if __spec__ is None else __spec__.name},sort_keys=True))
'''
        entry = self.root / 'entry.py'
        entry.write_text(source)
        for module in (None, 'entry'):
            original = self.requested(entry='./entry.py' if module is None else None,
                                      module=module, plain=True)
            plan = recipe.resolve_plan(original)
            recipe.verify(original, plan)
            native = subprocess.run([plan['python'], *plan['native_arguments']], cwd=plan['cwd'],
                                    capture_output=True, timeout=10)
            path = self.runtime / ('entry.journal' if module is None else 'module.journal')
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                process = subprocess.Popen(recipe.native_command(original, driver, descriptor),
                    cwd=plan['cwd'], pass_fds=(descriptor,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            finally:
                os.close(descriptor)
            try:
                out, err = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertEqual((process.returncode, out, err),
                             (native.returncode, native.stdout, native.stderr))
            receipt = journal.read(path, expected_pid=process.pid)
            self.assertIs(receipt.complete, module is None)
            if module is None:
                self.assertEqual(receipt.sources[0].content, source.encode())
            self.assertEqual(entry.read_text(), source)
            recipe.verify(original, plan)
