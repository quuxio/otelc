"""Genuine Rust prepared execution; reused by the public compiler/SDK suite."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from unittest.mock import patch

from otelc import control, native_gate, prepared_rust as prepared, rust_build, rust_owned_build
from otelc.config import Policy
from ci import rust_telemetry

APPLICATION = r'''use std::io::{self,BufRead};
#[used]
#[cfg_attr(target_os="macos", unsafe(link_section="__DATA,__mod_init_func,mod_init_funcs"))]
#[cfg_attr(not(target_os="macos"), unsafe(link_section=".init_array"))]
static INITIALIZER: extern "C" fn() = initialize;
extern "C" fn initialize(){if let Ok(path)=std::env::var("CTOR_MARKER"){
 std::fs::write(path,b"ctor\n").unwrap();}}
fn workload(x:i32)->i32{x*3+1}
fn main(){println!("ready {} {}",std::process::id(),std::env::args().nth(1).unwrap());
 for line in io::stdin().lock().lines(){let value:i32=line.unwrap().parse().unwrap();
 println!("{}",workload(value));}}
'''


class RustPreparedCases:
    """An ordinary original Rust fixture contains a constructor and literal argv."""

    def prepared_source(self) -> rust_build.Build:
        cls = type(self)
        if not hasattr(cls, '_prepared_source'):
            original = self.built.manifest()
            root = self.base / 'prepared-original'
            root.mkdir()
            (root / 'app.rs').write_text(APPLICATION)
            cls._prepared_source = rust_build.build(self.policy, root, Path('app.rs'),
                self.base / 'prepared-compiled', tools_directory=Path(original['tools']['path']).parent,
                sdk_directory=Path(original['sdk']['path']).parent)
        return cls._prepared_source

    def wait_prepared(self, path: Path, process: subprocess.Popen) -> dict:
        deadline = time.monotonic() + 25
        while True:
            self.assertIsNone(process.poll(), 'supervisor ended before ' + path.name)
            self.assertLess(time.monotonic(), deadline, 'prepared record deadline')
            try:
                return prepared.files.read(path)[0]
            except (FileNotFoundError, prepared.files.PublicationPending):
                time.sleep(.005)

    def prepared_child(self, owned: rust_owned_build.Build, current: Policy, runtime: Path, *,
                       plain: bool, marker: Path) -> tuple[subprocess.Popen[bytes], dict]:
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('preparation cannot start a process')):
            prepared.prepare(current, runtime, build_directory=owned.directory,
                             arguments=('literal $value; *',), plain=plain)
            command = prepared.command(runtime)
        process = subprocess.Popen(command, env=dict(os.environ, PATH='/no-tools', CTOR_MARKER=str(marker)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0)
        self.addCleanup(self.stop_prepared, runtime, process)
        app = self.wait_prepared(runtime / 'rust-application-start.json', process)
        self.assertEqual(app['parent_pid'], process.pid)
        self.assertEqual(os.getpgid(app['pid']), app['pid'])
        self.assertEqual(os.getsid(app['pid']), app['pid'])
        parent = subprocess.check_output(['/bin/ps', '-o', 'ppid=', '-p', str(app['pid'])], text=True).strip()
        self.assertEqual(parent, str(process.pid))
        self.assertFalse(select.select([process.stdout], [], [], 0)[0])
        self.assertFalse(marker.exists())
        self.assertFalse((runtime / 'app/control.json').exists())
        prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
        return process, app

    def stop_prepared(self, runtime: Path, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)

    def finish_prepared(self, runtime: Path, process: subprocess.Popen[bytes]) -> dict:
        out, err = process.communicate(timeout=15)
        self.assertEqual((process.returncode, out, err), (0, b'', b''))
        result = prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
        self.assertEqual(result['failures'], {})
        self.assertTrue(result['inputs_rechecked'])
        self.assertFalse(result['exec_verified'])
        return result

    def test_prepared_real_constructor_argv_and_same_pid_live_metric_states(self) -> None:
        source = self.prepared_source()
        data = copy.deepcopy(self.policy.data)
        data['resource']['service_name'] = 'otelc-rust-prepared-constructor'
        current = Policy.parse(data)
        before = native_gate.identity(source.directory / rust_build.MANIFEST)
        owned = rust_owned_build.build(current, source, self.base / 'prepared-owned')
        self.runtime.mkdir(mode=0o700)
        for plain in (True, False):
            runtime, marker = self.runtime / str(plain), self.runtime / (str(plain) + '.ctor')
            process, app = self.prepared_child(owned, current, runtime, plain=plain, marker=marker)
            self.assertEqual(self.line(process), f'ready {app["pid"]} literal $value; *\n'.encode())
            self.assertEqual(marker.read_bytes(), b'ctor\n')
            endpoint = runtime / 'app/control.json'
            for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
                before_points = len(rust_telemetry.points(self.capture, current.data['resource']['service_name']))
                if not plain:
                    control.wait_ready(endpoint, app['pid'])
                    control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                    self.assertEqual(control.request(endpoint, 'emission ' + ('on' if emission else 'off'))['pid'], app['pid'])
                process.stdin.write(b'7\n'); process.stdin.flush()
                self.assertEqual(self.line(process), b'22\n')
                if not plain:
                    control.request(endpoint, 'flush')
                    if not emission:
                        self.assertEqual(len(rust_telemetry.points(self.capture,
                            current.data['resource']['service_name'])), before_points)
                    if not collection:
                        self.assertEqual(rust_telemetry.calls(self.capture, current.data['resource']['service_name']), 0)
            result = self.finish_prepared(runtime, process)
            if plain:
                self.assertIsNone(result['sdk_health'])
                self.assertFalse((runtime / 'app/plan.json').exists())
                self.assertEqual(rust_telemetry.spans(self.capture, current.data['resource']['service_name']), [])
                self.assertEqual(rust_telemetry.points(self.capture, current.data['resource']['service_name']), [])
            else:
                self.assertEqual(result['sdk_health']['pid'], app['pid'])
                receipt = rust_telemetry.verify(self.capture, current, result['sdk_health'],
                    function='app.rs::workload', expected_spans=4, expected_calls=2)
                prepared.files.write(runtime / 'rust-telemetry-receipt.json', receipt)
        self.assertEqual(native_gate.identity(source.directory / rust_build.MANIFEST), before)

    def test_prepared_real_drop_profile_preserves_plain_and_instrumented_outcomes(self) -> None:
        source, current = self.lifetime_build()
        data = copy.deepcopy(current.data)
        data['resource']['service_name'] = 'otelc-rust-prepared-drop'
        current = Policy.parse(data)
        owned = rust_owned_build.build(current, source, self.base / 'prepared-drop-owned')
        self.runtime.mkdir(mode=0o700)
        for plain in (True, False):
            runtime = self.runtime / str(plain)
            process, app = self.prepared_child(owned, current, runtime, plain=plain,
                                               marker=self.runtime / (str(plain) + '.ctor'))
            self.assertEqual(self.line(process), b'ready\n')
            process.stdin.write(b'1\n'); process.stdin.flush()
            self.assertEqual(self.line(process), b'7 3\n')
            result = self.finish_prepared(runtime, process)
            if not plain:
                self.assertEqual(result['sdk_health']['schema_version'], 5)
                self.assertEqual(result['sdk_health']['drop_accounting']['completed'], '3')
                self.assertEqual(result['sdk_health']['pid'], app['pid'])
                receipt = rust_telemetry.verify(self.capture, current, result['sdk_health'],
                    function='LifetimeApp.rs::workload', expected_spans=1, expected_calls=1,
                    lifetimes={'Item lifetime': 1, 'Zero lifetime': 2})
                prepared.files.write(runtime / 'rust-telemetry-receipt.json', receipt)
            else:
                self.assertEqual(rust_telemetry.spans(self.capture, current.data['resource']['service_name']), [])
                self.assertEqual(rust_telemetry.points(self.capture, current.data['resource']['service_name']), [])

    def test_prepared_real_cli_runs_original_constructor_and_literal_arguments(self) -> None:
        source = self.prepared_source()
        owned = rust_owned_build.build(self.policy, source, self.base / 'prepared-cli-owned')
        configuration = self.base / 'prepared-cli.toml'
        configuration.write_text('schema_version=3\nlanguages=["rust"]\n'
            '[sources]\ninclude=["**"]\n[functions]\ninclude=["*workload*"]\n'
            '[resource]\nservice_name="otelc-rust-prepared-cli"\n'
            '[traces]\nenabled=true\nsample_ratio=1.0\n'
            '[export]\nendpoint="' + self.policy.data['export']['endpoint'] + '"\n'
            'interval_ms=60000\ntimeout_ms=1000\nshutdown_timeout_ms=5000\n')
        marker = self.base / 'prepared-cli.ctor'
        command = [str(rust_build.ROOT / 'bin/quux-otelc'), '--config', str(configuration),
            'rust-prepared-run', '--build', str(owned.directory), '--runtime', str(self.runtime),
            '--', 'literal $value; *']
        result = subprocess.run(command, env=dict(os.environ, PATH='/no-tools', OTELC_PYTHON=sys.executable,
            CTOR_MARKER=str(marker)), input=b'7\n', capture_output=True, timeout=45)
        self.assertEqual((result.returncode, result.stderr), (0, b''))
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], rb'^ready [0-9]+ literal \$value; \*$')
        self.assertEqual(lines[1], b'22')
        self.assertEqual(marker.read_bytes(), b'ctor\n')
        report = prepared.files.read(self.runtime / 'rust-execution.json')[0]
        self.assertEqual(report['failures'], {})
        self.assertTrue(report['released'])
        self.assertEqual(report['sdk_health']['pid'], report['application']['pid'])
        receipt = rust_telemetry.verify(self.capture, Policy.load(configuration), report['sdk_health'],
            function='app.rs::workload', expected_spans=1, expected_calls=1)
        prepared.files.write(self.runtime / 'rust-telemetry-receipt.json', receipt)
