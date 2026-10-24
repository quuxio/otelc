"""The separate handle compiler reads real class files without application startup."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import java, witness

ROOT = Path(__file__).resolve().parents[1]
PROFILE = 'java-sync-private-handle-v1'


class JavaHandleBindingTests(unittest.TestCase):
    def test_real_offline_compiler_binds_private_public_final_and_static_without_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'HandleFixture.java'
            source.write_text('''import java.lang.invoke.MethodHandle;
class HandleFixture {
  static final MethodHandle HANDLE = initialise();
  static MethodHandle initialise() {
    try { java.nio.file.Files.writeString(java.nio.file.Path.of(System.getenv("STARTUP_SENTINEL")),"started"); }
    catch (Exception failure) { throw new AssertionError("startup failed"); }
    throw new AssertionError("offline compiler started application");
  }
  private int privateTarget(int value) { return value+1; }
  public int publicTarget(int value) { return value+2; }
  public final int finalTarget(int value) { return value+3; }
  private static long staticTarget(long value) { return value+4; }
  static int wrapper(HandleFixture receiver,int value) throws Throwable { return (int)HANDLE.invokeExact(receiver,value); }
  static long staticWrapper(long value) throws Throwable { return (long)HANDLE.invokeExact(value); }
  static void rendezvous() {}
  static int envelope(HandleFixture receiver,int value) throws Throwable { rendezvous();return wrapper(receiver,value); }
  static long staticEnvelope(long value) throws Throwable { rendezvous();return staticWrapper(value); }
}
''')
            subprocess.run([java.executable('javac'), '-d', str(root), str(source)], check=True,
                           capture_output=True, timeout=20)
            binary = root / 'HandleFixture.class'
            originals = {path: path.read_bytes() for path in (source, binary)}
            sentinel = root / 'started'
            environment = {**os.environ, 'STARTUP_SENTINEL': str(sentinel)}
            extension = ROOT / 'adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar'
            helper = extension.with_name(extension.stem + '-helper-tool.jar')
            for target in ('privateTarget', 'publicTarget', 'finalTarget', 'staticTarget'):
                with self.subTest(target=target):
                    is_static = target == 'staticTarget'
                    call_descriptor = '(J)J' if is_static else '(LHandleFixture;I)I'
                    declarations = [('staticEnvelope' if is_static else 'envelope', call_descriptor),
                                    ('rendezvous', '()V'),
                                    ('staticWrapper' if is_static else 'wrapper', call_descriptor),
                                    (target, '(J)J' if is_static else '(I)I')]
                    arguments = [part for name, descriptor in declarations
                                 for part in (str(binary), 'HandleFixture', name, descriptor)]
                    command = [java.executable('java'), '-cp', os.pathsep.join(map(str, (extension, helper))),
                               'io.quux.otelc.witness.HandleBindings', *arguments]
                    result = subprocess.run(command, capture_output=True, env=environment, timeout=20)
                    self.assertEqual((result.returncode, result.stderr), (0, b''))
                    document = json.loads(result.stdout)
                    self.assertEqual(set(document), {'schema_version', 'kind', 'profile', 'bindings', 'route'})
                    self.assertEqual((document['schema_version'], document['profile']), (3, PROFILE))
                    route = document['route']
                    self.assertEqual(set(route), {'kind', 'field_owner', 'field_name', 'field_descriptor',
                        'site_instruction', 'invoke_owner', 'invoke_name', 'invoke_descriptor', 'reference_kind', 'sha256'})
                    self.assertEqual(route['reference_kind'], 6 if is_static else 5)
                    self.assertEqual(route['invoke_descriptor'], call_descriptor)
                    values = [PROFILE, route['kind'], route['field_owner'], route['field_name'],
                        route['field_descriptor'], str(route['site_instruction']), route['invoke_owner'],
                        route['invoke_name'], route['invoke_descriptor'], str(route['reference_kind']),
                        document['bindings']['wrapper']['probe_id'], document['bindings']['target']['probe_id']]
                    self.assertEqual(route['sha256'], hashlib.sha256('\n'.join(values).encode()).hexdigest())
                    output = root / (target + '.json')
                    output.write_bytes(result.stdout)
                    with self.assertRaisesRegex(ValueError, 'schema 2'):
                        witness.load_bindings(output, mock.Mock())
                    direct = subprocess.run(command[:3] + ['io.quux.otelc.witness.WitnessBindings', *arguments],
                                            capture_output=True, env=environment, timeout=20)
                    self.assertNotEqual(direct.returncode, 0)
                    self.assertIn(b'wrapper body', direct.stderr)
                    self.assertFalse(sentinel.exists())
                    self.assertEqual({path: path.read_bytes() for path in originals}, originals)
