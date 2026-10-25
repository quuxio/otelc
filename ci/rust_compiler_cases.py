"""Explicit internal Rust compiler/SDK tests; not public language admission."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import unittest

from otelc import control, health, native_build, native_launch
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = '''use std::io::{self,BufRead};
mod work;
fn main(){println!("ready");for line in io::stdin().lock().lines(){
 let value:i32=line.unwrap().parse().unwrap();
 let result=match std::panic::catch_unwind(||work::workload(value)){
 Ok(value)=>value,Err(payload)=>{assert_eq!(payload.downcast_ref::<u32>(),Some(&73));-73}};
 println!("{} {}",result,work::drops());}}
'''
WORK = '''use std::sync::atomic::{AtomicU32,Ordering};
static DROPS:AtomicU32=AtomicU32::new(0);
struct Guard;
impl Drop for Guard {fn drop(&mut self){DROPS.fetch_add(1,Ordering::SeqCst);}}
pub fn drops()->u32{DROPS.load(Ordering::SeqCst)}
pub fn workload(value:i32)->i32{let _guard=Guard;
 if value<0 {std::panic::panic_any(73_u32)}
 let mut sum=0;for index in 0..value{sum+=leaf(index);}sum}
fn leaf(value:i32)->i32{value*3+1}
'''

# Deliberately tool-owned private-ABI harness. These explicit calls qualify the
# bridge, not automatic insertion into ordinary Rust developer source.
DROP_HARNESS = r'''use otelc_rust_bridge::compiler_mir as hooks;
use std::io::{self,BufRead};
use std::mem::ManuallyDrop;
use std::sync::atomic::{AtomicU32,Ordering};
const USR:&str="rust:private:Item\0";
const NAME:&str="PrivateItem\0";
static DROPS:AtomicU32=AtomicU32::new(0);
struct Item {value:i32,fail:bool}
impl Drop for Item {fn drop(&mut self){
 DROPS.fetch_add(1,Ordering::SeqCst);
 if self.fail {std::panic::panic_any(73_u32)}
}}
fn action(command:i32)->i32 {
 if command==6 {
  let long=format!("{}\0","x".repeat(4097));
  for bad in ["","\0","unterminated","a\0b\0",long.as_str()] {
   hooks::drop_created(1,bad,NAME);hooks::drop_created(1,USR,bad);
   hooks::drop_moved(1,2,bad);assert_eq!(hooks::drop_enter(1,bad),0);
  }
  hooks::drop_exit(0);hooks::drop_unwind(0);return 0
 }
 let frame=hooks::sync_enter("private-drop-action");
 let mut source=ManuallyDrop::new(Item{value:command*7,fail:command==2});
 let before=(&*source as *const Item) as usize;
 hooks::drop_created(before,USR,NAME);
 std::thread::sleep(std::time::Duration::from_millis(1));
 let mut destination=Box::new(ManuallyDrop::new(unsafe{ManuallyDrop::take(&mut source)}));
 let after=(&**destination as *const Item) as usize;
 assert_ne!(before,after);hooks::drop_moved(before,after,USR);hooks::drop_moved(after,after,USR);
 let value=destination.value;
 let result=if command==3 {std::mem::forget(destination);value} else {
  let token=hooks::drop_enter(after,USR);assert_ne!(token,0);
  match std::panic::catch_unwind(std::panic::AssertUnwindSafe(||unsafe{
   ManuallyDrop::drop(&mut destination)
  })) {
   Ok(())=>{hooks::drop_exit(token);value},
   Err(payload)=>{hooks::drop_unwind(token);assert_eq!(payload.downcast_ref::<u32>(),Some(&73));-73}
  }
 };
 hooks::sync_exit(frame);result
}
fn main(){println!("ready");for line in io::stdin().lock().lines(){
 let command:i32=line.unwrap().parse().unwrap();
 let result=action(command);println!("{} {}",result,DROPS.load(Ordering::SeqCst));
}}
'''


def digest(path: Path) -> str:
    """Bind retained original inputs and actual compiler/runtime products."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RustCompilerCases(unittest.TestCase):
    """Run only with explicit matching products; never silently skip this suite."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.output = Path(os.environ['OTELC_RUST_COMPONENT_OUTPUT']).resolve()
        cls.output.mkdir(mode=0o700, exist_ok=False)
        cls.driver = Path(os.environ['OTELC_RUST_COMPILER']).resolve(strict=True)
        cls.bridge = Path(os.environ['OTELC_RUST_BRIDGE']).resolve(strict=True)
        cls.rustc = Path(os.environ['OTELC_RUSTC']).resolve(strict=True)
        cls.sdk = Path(os.environ['OTELC_NATIVE_SDK_BUILD']).resolve(strict=True)
        cls.sdk_record = native_build.sdk(cls.sdk)
        cls.sysroot = subprocess.check_output([str(cls.rustc), '--print', 'sysroot'], text=True).strip()
        version = subprocess.check_output([str(cls.rustc), '-vV'], text=True)
        if 'release: 1.98.1\n' not in version or 'LLVM version: 22.1.8\n' not in version:
            raise ValueError('Rust compiler component requires the exact 1.98.1/LLVM22.1.8 profile')
        cls.held = {str(path): digest(path) for path in
                    (cls.driver, cls.bridge, cls.rustc, cls.sdk / native_build.SDK_MANIFEST,
                     Path(cls.sdk_record['library']['path']), Path(__file__), ROOT / 'otelc/health.py')}
        (cls.output / 'tool-inputs.json').write_text(json.dumps(cls.held, indent=2) + '\n')
        (cls.output / 'rustc-version.txt').write_text(version)
        bootstrap = cls.output / 'bootstrap.cpp'
        bootstrap.write_text(native_launch.BOOTSTRAP)
        command = [cls.sdk_record['toolchain']['directory'] + '/clang++', '-std=c++17', '-O2',
                   '-c', str(bootstrap), '-I', str(ROOT / 'adapters/native/launcher/include'),
                   '-o', str(cls.output / 'bootstrap.o')]
        subprocess.run(command, check=True, capture_output=True, timeout=60)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.held != {name: digest(Path(name)) for name in cls.held}:
            raise AssertionError('Rust compiler, bridge or genuine SDK inputs changed')
        if native_build.sdk(cls.sdk) != cls.sdk_record:
            raise AssertionError('Rust SDK source bindings changed')

    def setUp(self) -> None:
        self.base = self.output / self._testMethodName
        self.base.mkdir()
        self.root = self.base / 'application'
        self.root.mkdir()
        self.sources: dict[str, str] = {}
        self.counter = 0
        self.capture = Capture()
        self.addCleanup(self.capture.close)

    def tearDown(self) -> None:
        self.assertEqual(self.sources, {name: digest(Path(name)) for name in self.sources})
        (self.base / 'original-sources.json').write_text(json.dumps(self.sources, indent=2) + '\n')
        for kind in ('traces', 'metrics'):
            for index, message in enumerate(getattr(self.capture, kind)):
                (self.base / f'decoded-{kind}-{index}.protobuf').write_bytes(message.SerializeToString())

    def source(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text)
        self.sources[str(path)] = digest(path)
        return path

    def policy(self, *, collection: bool = True, emission: bool = True) -> Policy:
        return Policy.parse({'schema_version': 3, 'languages': ['rust'],
            'sources': {'include': ['**']}, 'functions': {'include': ['*workload*']},
            'metrics': {'collection': collection, 'emission': emission},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 60000, 'timeout_ms': 1000, 'shutdown_timeout_ms': 5000}})

    def command(self, arguments: list[str], *, request: dict | None = None) -> subprocess.CompletedProcess:
        self.counter += 1
        name = self.base / f'command-{self.counter}'
        environment = dict(os.environ, RUST_BACKTRACE='0', LLVM_PROFILE_FILE=str(self.output / '%m-%p.profraw'))
        environment.pop('OTELC_RUST_REQUEST', None)
        if request is not None:
            filename = name.with_suffix('.request.json')
            filename.write_text(json.dumps(request) + '\n')
            environment['OTELC_RUST_REQUEST'] = str(filename)
        result = subprocess.run(arguments, cwd=self.base, env=environment,
                                capture_output=True, timeout=120)
        name.with_suffix('.stdout').write_bytes(result.stdout)
        name.with_suffix('.stderr').write_bytes(result.stderr)
        name.with_suffix('.json').write_text(json.dumps({'arguments': arguments,
            'exit_code': result.returncode}, indent=2) + '\n')
        return result

    def compile(self, entry: Path, mode: str, *, selected: tuple[str, ...] = (),
                optimisation: int = 2, bridge: Path | None = None,
                request_changes: dict | None = None) -> tuple[subprocess.CompletedProcess, Path, Path]:
        name = self.base / f'build-{self.counter}'
        inventory, product = name.with_suffix('.json'), name.with_name(name.name + '-application')
        request = {'schema_version': 1, 'mode': mode, 'root': str(self.root), 'entry': str(entry),
                   'sources': self.sources, 'selected': list(selected), 'output': str(inventory)}
        request.update(request_changes or {})
        arguments = [str(self.driver), str(entry), '--crate-name', 'component', '--edition=2024',
                     '--sysroot', self.sysroot, '-Copt-level=' + str(optimisation), '-g', '-o', str(product)]
        if mode == 'weave':
            library = bridge or self.bridge
            arguments += ['--extern', 'otelc_rust_bridge=' + str(library),
                '-L', 'dependency=' + str(library.parent / 'deps'),
                '-L', 'native=' + str(self.sdk), '-l', 'dylib=otelc_native_runtime',
                '-C', 'link-arg=' + str(self.output / 'bootstrap.o'),
                '-C', 'link-arg=-Wl,-rpath,' + str(self.sdk)]
        return self.command(arguments, request=request), inventory, product

    def selected(self, entry: Path) -> tuple[str, ...]:
        result, inventory, product = self.compile(entry, 'inspect')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(product.exists())
        rows = json.loads(inventory.read_bytes())['definitions']
        policy = self.policy()
        return tuple(row['identity'] for row in rows if
            policy.selection('functions').accepts(row['compiler_name']) and
            policy.selection('sources').accepts(str(Path(row['file']).relative_to(self.root))))

    def start(self, product: Path, label: str, policy: Policy,
              *, drops: bool = False) -> tuple[subprocess.Popen, Path]:
        runtime = self.base / label
        runtime.mkdir(mode=0o700)
        data = policy.data
        settings = dict(data['export']) | {'service_name': label,
            **{key: data['metrics'][key] for key in ('collection', 'emission')},
            'boundaries': data['metrics']['histogram_boundaries_seconds'], 'traces': True, 'sample_ratio': 1.0}
        if drops:
            settings['drop_lifetimes'] = {'enabled': True, 'max_live': 16}
        plan = {'schema_version': 3 if drops else 2, 'language': 'rust', 'runtime': str(runtime),
                'policy_sha256': native_launch.digest(data),
                'selection_sha256': native_launch.digest({key: data[key] for key in ('sources', 'functions')}),
                'token': 'c' * 64, 'settings': settings}
        descriptor = os.open(runtime / 'plan.json', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.write(descriptor, json.dumps(plan).encode())
            process = subprocess.Popen([str(product)], cwd=self.base, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(descriptor,), bufsize=0,
                env=dict(os.environ, OTELC_NATIVE_PLAN_FD=str(descriptor), RUST_BACKTRACE='0',
                         LLVM_PROFILE_FILE=str(self.output / '%m-%p.profraw')))
        finally:
            os.close(descriptor)
        self.addCleanup(self.clean_process, process, runtime)
        self.assertEqual(self.line(process), b'ready\n')
        endpoint = runtime / 'control.json'
        control.wait_ready(endpoint, process.pid)
        return process, endpoint

    @staticmethod
    def clean_process(process: subprocess.Popen, runtime: Path) -> None:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=10)
        (runtime / 'remaining.stdout').write_bytes(stdout)
        (runtime / 'stderr').write_bytes(stderr)
        (runtime / 'process-result.json').write_text(json.dumps({'exit_code': process.returncode}) + '\n')

    def line(self, process: subprocess.Popen) -> bytes:
        self.assertTrue(select.select([process.stdout], [], [], 15)[0], 'Rust response timed out')
        return process.stdout.readline()

    def call(self, process: subprocess.Popen, value: int, expected: bytes) -> None:
        process.stdin.write(f'{value}\n'.encode())
        process.stdin.flush()
        self.assertEqual(self.line(process), expected)

    def test_original_modules_optimised_loops_panics_and_all_live_metric_states(self) -> None:
        entry = self.source('app.rs', APPLICATION)
        self.source('work.rs', WORK)
        selected = self.selected(entry)
        self.assertEqual(selected, ('work.rs::work::workload',))
        for optimisation in (0, 3):
            result, _, woven = self.compile(entry, 'weave', selected=selected, optimisation=optimisation)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            stock = self.base / f'stock-{optimisation}'
            result = self.command([str(self.rustc), str(entry), '--edition=2024',
                '-Copt-level=' + str(optimisation), '-o', str(stock)])
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            reference = subprocess.run([str(stock)], input=b'3\n2\n-1\n4\n-1\n', capture_output=True,
                env=dict(os.environ, RUST_BACKTRACE='0'), timeout=15)
            self.assertEqual(reference.returncode, 0)
            self.assertEqual(reference.stdout, b'ready\n12 1\n5 2\n-73 3\n22 4\n-73 5\n')
            for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
                label = f'rust-{optimisation}-{int(collection)}{int(emission)}'
                self.exercise(woven, label, self.policy(collection=collection, emission=emission), reference.stderr)

    def exercise(self, woven: Path, label: str, policy: Policy, expected_stderr: bytes) -> None:
        process, endpoint = self.start(woven, label, policy)
        self.call(process, 3, b'12 1\n')
        control.request(endpoint, 'flush')
        control.request(endpoint, 'collection on')
        control.request(endpoint, 'emission off')
        self.call(process, 2, b'5 2\n')
        exports = control.request(endpoint, 'health')['metric_exports']['batches_started']
        control.request(endpoint, 'flush')
        self.assertEqual(control.request(endpoint, 'health')['metric_exports']['batches_started'], exports)
        control.request(endpoint, 'emission on')
        control.request(endpoint, 'flush')
        control.request(endpoint, 'collection off')
        self.call(process, -1, b'-73 3\n')
        control.request(endpoint, 'collection on')
        self.call(process, 4, b'22 4\n')
        self.call(process, -1, b'-73 5\n')
        self.assertEqual(control.request(endpoint, 'status')['pid'], process.pid)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((process.returncode, stdout), (0, b''))
        self.assertEqual(self.panic_output(stderr), self.panic_output(expected_stderr))
        final = json.loads((endpoint.parent / 'health.json').read_bytes())
        self.assertEqual((final['language'], final['phase']), ('rust', 'final'))
        counts = final['function_accounting']
        self.assertEqual({key: counts[key] for key in ('registered', 'started', 'ended', 'active', 'lost', 'invalid')},
                         dict(registered='1', started='5', ended='5', active='0', lost='0', invalid='0'))
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 if any(item.key == 'service.name' and item.value.string_value == label for item in resource.resource.attributes)
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 5)
        self.assertTrue(all(span.name == 'work.rs::work::workload' for span in spans))
        self.assertEqual(sum(span.status.code == 2 for span in spans), 2)
        points = self.metric_points(label)
        self.assertEqual(points['otelc.function.calls'], 4 if policy.data['metrics']['collection'] else 3)
        self.assertEqual(points['otelc.function.errors'], 1)

    def metric_points(self, label: str) -> dict[str, int]:
        resources = [resource for request in self.capture.metrics for resource in request.resource_metrics
                     if any(item.key == 'service.name' and item.value.string_value == label
                            for item in resource.resource.attributes)]
        metrics = [metric for resource in resources for scope in resource.scope_metrics for metric in scope.metrics]
        return {metric.name: sum(point.as_int for point in metric.sum.data_points) for metric in metrics
                if metric.name in ('otelc.function.calls', 'otelc.function.errors')}

    def panic_output(self, raw: bytes) -> bytes:
        # Rust's default hook prints a different OS thread ID in each process.
        # Keep its original source position, payload text and every other byte.
        value, count = re.subn(rb"(?m)^(thread 'main' )\([0-9]+\)( panicked at )",
                               rb'\1(<thread-id>)\2', raw)
        self.assertEqual(count, 2)
        return value

    def test_private_drop_bridge_moves_panics_forgets_and_refuses_bad_metadata(self) -> None:
        entry = self.source('private_drop.rs', DROP_HARNESS)
        result, _, product = self.compile(entry, 'weave')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
            label = f'private-drop-{int(collection)}{int(emission)}'
            self.exercise_drop(product, label, collection, emission)

    def exercise_drop(self, product: Path, label: str, collection: bool, emission: bool) -> None:
        """Qualify the explicit bridge with real Rust drop side effects and SDK receipt."""
        process, endpoint = self.start(product, label,
            self.policy(collection=collection, emission=emission), drops=True)
        self.call(process, 1, b'7 1\n')
        control.request(endpoint, 'flush')
        control.request(endpoint, 'collection on'); control.request(endpoint, 'emission off')
        self.call(process, 2, b'-73 2\n')
        before = control.request(endpoint, 'health')['metric_exports']['batches_started']
        control.request(endpoint, 'flush')
        self.assertEqual(control.request(endpoint, 'health')['metric_exports']['batches_started'], before)
        control.request(endpoint, 'collection off'); control.request(endpoint, 'emission on')
        self.call(process, 3, b'21 2\n')
        control.request(endpoint, 'on')
        self.call(process, 1, b'7 3\n'); self.call(process, 3, b'21 3\n')
        self.call(process, 6, b'0 3\n')
        self.assertEqual(control.request(endpoint, 'status')['pid'], process.pid)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((process.returncode, stdout), (0, b''))
        self.assertEqual(stderr.count(b'panicked at'), 1)
        final = health.read(endpoint.parent / 'health.json')
        self.assertEqual((final['schema_version'], final['pid']), (5, process.pid))
        self.assertEqual(final['lifecycle']['shutdown'], 'succeeded')
        self.assertEqual(final['function_accounting']['metadata_errors'], '20')
        self.assertEqual(final['drop_accounting'], dict(types='1', admitted='5', completed='3',
            live='0', dropping='0', ending='0', censored='2', lost='0', duplicates='0', invalid='0',
            closing=True, closed=True))
        self.check_drop_spans(label)
        self.check_drop_metrics(label, collection)

    def check_drop_spans(self, label: str) -> None:
        """Join independent lifetime traces to their actual action span identities."""
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 if any(item.key == 'service.name' and item.value.string_value == label
                        for item in resource.resource.attributes)
                 for scope in resource.scope_spans for span in scope.spans]
        functions = {span.span_id for span in spans if span.name == 'private-drop-action'}
        lifetimes = [span for span in spans if span.name == 'PrivateItem lifetime']
        self.assertEqual((len(spans), len(functions), len(lifetimes)), (10, 5, 5))
        complete = []
        for span in lifetimes:
            self.assertFalse(span.parent_span_id)
            self.assertEqual(len(span.links), 1)
            self.assertIn(span.links[0].span_id, functions)
            labels = {item.key: item.value for item in span.attributes}
            self.assertNotIn('otelc.destructor.exception_exit', labels)
            if labels['boundary'].string_value == 'drop_entry':
                complete.append(labels['otelc.drop.exception_exit'].bool_value)
            else:
                self.assertEqual(labels['boundary'].string_value, 'shutdown_censored')
        self.assertEqual(sorted(complete), [False, False, True])

    def check_drop_metrics(self, label: str, collection: bool) -> None:
        """Require retained collection and absence of retroactive admission metrics."""
        metrics = [metric for request in self.capture.metrics for resource in request.resource_metrics
                   if any(item.key == 'service.name' and item.value.string_value == label
                          for item in resource.resource.attributes)
                   for scope in resource.scope_metrics for metric in scope.metrics]
        for name, expected in (('admitted', 4 if collection else 3),
                               ('completed', 3 if collection else 2), ('incomplete', 1)):
            points = [sum(point.as_int for point in metric.sum.data_points)
                      for metric in metrics if metric.name == 'otelc.lifetime.' + name]
            self.assertTrue(points, name)
            self.assertEqual(max(points), expected, name)

    def test_wrong_compiler_entry_is_refused_in_both_modes(self) -> None:
        entry = self.source('app.rs', 'fn main(){}\n')
        other = self.source('other.rs', 'fn main(){}\n')
        for mode in ('inspect', 'weave'):
            result, inventory, product = self.compile(entry, mode, request_changes={'entry': str(other)})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'compiler crate root differs', result.stderr)
            self.assertFalse(inventory.exists())
            self.assertFalse(product.exists())

    def test_unbound_module_and_unknown_selection_are_refused(self) -> None:
        entry = self.source('app.rs', 'mod work; fn main(){work::workload();}\n')
        module = self.source('work.rs', 'pub fn workload(){}\n')
        result, inventory, product = self.compile(entry, 'inspect', request_changes={
            'sources': {str(entry): self.sources[str(entry)]}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'compiler read an unbound Rust source', result.stderr)
        self.assertFalse(inventory.exists())
        self.assertFalse(product.exists())
        result, inventory, product = self.compile(entry, 'weave', selected=('work.rs::missing',))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'missing, duplicate or excessive Rust compiler identities', result.stderr)
        self.assertFalse(inventory.exists())
        self.assertFalse(product.exists())
        self.assertEqual(digest(module), self.sources[str(module)])

    def test_async_and_expanded_selected_bodies_refuse_before_codegen(self) -> None:
        entry = self.source('app.rs', 'async fn workload_async()->u32{7}\n'
            'macro_rules! make_fn {()=>{fn workload_macro()->u32{9}}}\n'
            'make_fn!(); fn main(){std::hint::black_box(workload_macro());}\n')
        result, inventory, _ = self.compile(entry, 'inspect')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        rows = json.loads(inventory.read_bytes())['definitions']
        unsupported = [row for row in rows if row['asynchronous'] or row['expanded']]
        self.assertGreaterEqual(len(unsupported), 2)
        for row in unsupported:
            result, _, product = self.compile(entry, 'weave', selected=(row['identity'],))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'async/macro', result.stderr)
            self.assertIn(b'separate', result.stderr)
            self.assertFalse(product.exists())

    def test_incompatible_bridge_hooks_refuse_without_a_compiler_crash(self) -> None:
        entry = self.source('app.rs', 'fn workload()->u64{7} fn main(){println!("{}",workload());}\n')
        selected = self.selected(entry)
        enter = 'pub fn sync_enter(_: &str)->u64{0}'
        exit = 'pub fn sync_exit(_:u64){}'
        unwind = 'pub fn sync_unwind(_:u64){}'
        def module(*items: str) -> str:
            return 'pub mod compiler_mir {' + ' '.join(items) + '}\n'
        variants = (
            ('argument', module('pub fn sync_enter(_:u8)->u64{0}', exit, unwind), 'incompatible signature'),
            ('result', module('pub fn sync_enter(_: &str)->u32{0}', exit, unwind), 'incompatible signature'),
            ('arity', module('pub fn sync_enter()->u64{0}', exit, unwind), 'incompatible signature'),
            ('completion', module(enter, 'pub fn sync_exit(_:u32){}', unwind), 'incompatible signature'),
            ('unwind', module(enter, exit, 'pub fn sync_unwind(_:u64)->u8{0}'), 'incompatible signature'),
            ('generic', module('pub fn sync_enter<T>(_: &str)->u64{0}', exit, unwind), 'incompatible definition'),
            ('missing', module(), 'SDK hook sync_enter is missing'),
            ('module', 'pub fn compiler_mir(){}', 'compiler_mir must be a module'),
            ('nomodule', 'pub fn unrelated(){}', 'SDK hooks are missing'),
        )
        for name, body, message in variants:
            source = self.base / (name + '.rs')
            source.write_text(body)
            bridge = self.base / ('lib' + name + '.rlib')
            result = self.command([str(self.rustc), str(source), '--edition=2024',
                '--crate-name', 'otelc_rust_bridge', '--crate-type', 'rlib', '-o', str(bridge)])
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            result, _, product = self.compile(entry, 'weave', selected=selected, bridge=bridge)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b'internal compiler error', result.stderr)
            self.assertIn(message.encode(), result.stderr)
            self.assertFalse(product.exists())


def main() -> int:
    """Retain exact test counts so a skipped or missing group cannot pass."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RustCompilerCases)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('test_health.HealthShapeTests'))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName('test_rust_drop_health'))
    selected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    value = {'selected': selected, 'tests_run': result.testsRun, 'skipped': len(result.skipped),
             'failures': len(result.failures), 'errors': len(result.errors), 'successful': result.wasSuccessful()}
    (Path(os.environ['OTELC_RUST_COMPONENT_OUTPUT']) / 'tests-result.json').write_text(json.dumps(value, indent=2) + '\n')
    return 0 if selected >= 10 and result.testsRun == selected and not result.skipped and result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
