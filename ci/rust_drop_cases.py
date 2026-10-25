"""Real unchanged-source Rust MIR lifetime cases, using the shared SDK fixture."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

from otelc import control, health, rust_lifetimes
from otelc.config import Policy

WORK = '''use std::sync::atomic::{AtomicU32,Ordering};
static DROPS:AtomicU32=AtomicU32::new(0);
pub struct Item {value:i32,fail:bool}
impl Drop for Item {fn drop(&mut self){
 DROPS.fetch_add(1,Ordering::SeqCst);
 if self.fail {std::panic::panic_any(73_u32)}
}}
pub struct Zero;
impl Drop for Zero {fn drop(&mut self){DROPS.fetch_add(1,Ordering::SeqCst);}}
pub fn drops()->u32{DROPS.load(Ordering::SeqCst)}
pub fn workload(command:i32)->i32 {
 let item=Item{value:command*7,fail:command==2};
 let first=Zero;let second=Zero;let moved=item;
 std::hint::black_box((&moved,&first,&second));
 if command==3 {std::panic::panic_any(73_u32)}
 moved.value
}
'''


class RustAutomaticDropCases:
    """Mixin: original compiler/SDK tools and process ownership come from the suite."""

    def lifetime_policy(self, collection: bool = True, emission: bool = True) -> Policy:
        data = copy.deepcopy(self.policy(collection=collection, emission=emission).data)
        data['lifetimes'].update(enabled=True, include=['*Item*', '*Zero'])
        return Policy.parse(data)

    def lifetime_request(self, entry: Path, policy: Policy, *, optimisation: int = 2) -> dict:
        result, inventory, product = self.compile(entry, 'lifetime-inspect', optimisation=optimisation)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(product.exists())
        return rust_lifetimes.weave_request(inventory, policy, self.root, self.sources)

    def test_automatic_drop_mir_preserves_stock_moves_zero_size_and_panic_paths(self) -> None:
        application = 'use std::io::{self,BufRead};mod work;\n' \
            'fn main(){println!("ready");for line in io::stdin().lock().lines(){\n' \
            'let value:i32=line.unwrap().parse().unwrap();\n' \
            'let result=match std::panic::catch_unwind(||work::workload(value)){\n' \
            'Ok(value)=>value,Err(p)=>{assert_eq!(p.downcast_ref::<u32>(),Some(&73));-73}};\n' \
            'println!("{} {}",result,work::drops());}}\n'
        entry = self.source('app.rs', application)
        self.source('work.rs', WORK)
        selected = self.selected(entry)
        for optimisation in (0, 3):
            requested = self.lifetime_request(entry, self.lifetime_policy(), optimisation=optimisation)
            bindings = requested['lifetimes']
            self.assertEqual(set(bindings), {'work.rs::work::workload'})
            self.assertGreaterEqual(len(next(iter(bindings.values()))), 4)
            result, inventory, product = self.compile(entry, 'lifetime-weave', selected=selected,
                optimisation=optimisation, request_changes=requested)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            woven = json.loads(inventory.read_bytes())
            self.assertEqual(woven['kind'], 'otelc.rust.lifetime-weave')
            self.assertEqual(woven['lifetimes'], bindings)
            self.assertEqual(woven['lifetime_bodies'], requested['lifetime_bodies'])
            self.assertFalse(woven['instrumentation_complete'])
            stock = self.base / f'automatic-stock-{optimisation}'
            result = self.command([str(self.rustc), str(entry), '--edition=2024',
                '-Copt-level=' + str(optimisation), '-C', 'instrument-coverage', '-o', str(stock)])
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            reference = subprocess.run([str(stock)], input=b'1\n2\n3\n1\n', capture_output=True,
                env=dict(os.environ, RUST_BACKTRACE='0'), timeout=15)
            self.assertEqual((reference.returncode, reference.stdout),
                (0, b'ready\n7 3\n-73 6\n-73 9\n7 12\n'))
            for collection, emission in ((False, False), (False, True), (True, False), (True, True)):
                label = f'automatic-drop-{optimisation}-{int(collection)}{int(emission)}'
                self.exercise_automatic_drop(product, label,
                    self.lifetime_policy(collection, emission), reference.stderr)

    def exercise_automatic_drop(self, product: Path, label: str, policy: Policy, expected_stderr: bytes) -> None:
        process, endpoint = self.start(product, label, policy, drops=True)
        self.call(process, 1, b'7 3\n')
        control.request(endpoint, 'collection on'); control.request(endpoint, 'emission off')
        self.call(process, 2, b'-73 6\n')
        before = control.request(endpoint, 'health')['metric_exports']['batches_started']
        control.request(endpoint, 'flush')
        self.assertEqual(control.request(endpoint, 'health')['metric_exports']['batches_started'], before)
        control.request(endpoint, 'collection off'); control.request(endpoint, 'emission on')
        self.call(process, 3, b'-73 9\n')
        control.request(endpoint, 'on')
        self.call(process, 1, b'7 12\n')
        self.assertEqual(control.request(endpoint, 'status')['pid'], process.pid)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual((process.returncode, stdout), (0, b''))
        self.assertEqual(self.panic_output(stderr), self.panic_output(expected_stderr))
        final = health.read(endpoint.parent / 'health.json')
        self.assertEqual(final['lifecycle']['shutdown'], 'succeeded')
        self.assertEqual(final['function_accounting']['metadata_errors'], '0')
        self.assertEqual(final['drop_accounting'], dict(types='2', admitted='12', completed='12',
            live='0', dropping='0', ending='0', censored='0', lost='0', duplicates='0', invalid='0',
            closing=True, closed=True))
        self.check_automatic_spans(label)
        self.assertEqual(self.metric_points(label)['otelc.function.calls'],
                         3 if policy.data['metrics']['collection'] else 2)

    def check_automatic_spans(self, label: str) -> None:
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 if any(item.key == 'service.name' and item.value.string_value == label
                        for item in resource.resource.attributes)
                 for scope in resource.scope_spans for span in scope.spans]
        functions = {span.span_id for span in spans if span.name == 'work.rs::work::workload'}
        lifetimes = [span for span in spans if span.name.endswith(' lifetime')]
        self.assertEqual((len(spans), len(functions), len(lifetimes)), (16, 4, 12))
        exceptional = []
        for span in lifetimes:
            self.assertFalse(span.parent_span_id)
            self.assertEqual(len(span.links), 1)
            self.assertIn(span.links[0].span_id, functions)
            labels = {item.key: item.value for item in span.attributes}
            self.assertEqual(labels['boundary'].string_value, 'drop_entry')
            self.assertNotIn('otelc.destructor.exception_exit', labels)
            exceptional.append(labels['otelc.drop.exception_exit'].bool_value)
        self.assertEqual(sorted(exceptional), [False] * 11 + [True])

    def test_automatic_drop_mir_refuses_unsupported_ownership_and_changed_bindings(self) -> None:
        prefix = 'struct Item;impl Drop for Item {fn drop(&mut self){}}\n'
        cases = (
            ('escape', prefix + 'fn workload(){let item=Item;std::mem::forget(item);}'
             'fn main(){workload();}', 'ownership escape'),
            ('factory', prefix + 'fn make()->Item{Item}'
             'fn workload(){let item=make();std::hint::black_box(&item);}fn main(){workload();}',
             'ownership escape'),
            ('argument', prefix + 'fn workload(item:Item){std::hint::black_box(&item);}'
             'fn main(){workload(Item);}', 'arguments/returns'),
            ('no_drop', 'struct Item;fn workload(){let item=Item;std::hint::black_box(&item);}'
             'fn main(){workload();}', 'type/layout/drop binding differs'),
            ('generic', 'struct Item<T>(T);impl<T> Drop for Item<T>{fn drop(&mut self){}}'
             'fn workload<T>(value:T){let item=Item(value);std::hint::black_box(&item);}'
             'fn main(){workload(7_u32);}', 'type/layout/drop binding differs'),
        )
        for name, source, message in cases:
            with self.subTest(case=name):
                entry = self.source(name + '.rs', source)
                policy = self.lifetime_policy()
                # Test a precise private binding refusal without hiding any
                # rejected selection behind a successful partial build.
                requested = self.lifetime_request(entry, policy)
                bindings = {key: value for key, value in requested['lifetimes'].items()
                            if key.endswith('::workload')}
                requested.update(lifetimes=bindings, lifetime_bodies={key: requested['lifetime_bodies'][key]
                                 for key in bindings})
                self.assertTrue(bindings)
                result, inventory, product = self.compile(entry, 'lifetime-weave',
                    request_changes=requested)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message.encode(), result.stderr)
                self.assertNotIn(b'internal compiler error', result.stderr)
                self.assertFalse(product.exists()); self.assertFalse(inventory.exists())

        entry = self.source('bound.rs', prefix + 'fn workload(){let item=Item;'
                            'std::hint::black_box(&item);}fn main(){workload();}')
        original = self.lifetime_request(entry, self.lifetime_policy())
        body, locals_ = next(iter(original['lifetimes'].items()))
        local, typename = next(iter(locals_.items()))
        for changed, message in (({body: {local: 'WrongType'}}, 'binding differs'),
                                 ({body: {'65535': typename}}, 'local is missing'),
                                 ({'bound.rs::missing': locals_}, 'binding is missing')):
            with self.subTest(changed=changed):
                result, inventory, product = self.compile(entry, 'lifetime-weave',
                    request_changes=original | {'lifetimes': changed, 'lifetime_bodies':
                        {name: original['lifetime_bodies'][body] for name in changed}})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message.encode(), result.stderr)
                self.assertFalse(product.exists()); self.assertFalse(inventory.exists())

    def test_automatic_drop_requires_matching_typed_private_sdk_hooks(self) -> None:
        entry = self.source('hooks.rs', 'struct Item;impl Drop for Item{fn drop(&mut self){}}'
            'fn workload(){let item=Item;std::hint::black_box(&item);}fn main(){workload();}')
        requested = self.lifetime_request(entry, self.lifetime_policy())
        valid = {'new': 'pub fn drop_new(_: &str,_: &str)->usize{1}',
            'enter': 'pub fn drop_enter(_:usize,_: &str)->u64{1}',
            'exit': 'pub fn drop_exit(_:u64){}', 'unwind': 'pub fn drop_unwind(_:u64){}'}
        variants = (('new', 'pub fn drop_new(_:u8,_: &str)->usize{1}'),
            ('new', 'pub fn drop_new(_: &str,_: &str)->u64{1}'),
            ('enter', 'pub fn drop_enter(_:u64,_: &str)->u64{1}'),
            ('exit', 'pub fn drop_exit(_:u64)->u8{0}'),
            ('unwind', 'pub fn drop_unwind(_:usize){}'))
        for index, (key, replacement) in enumerate(variants):
            source = self.base / f'bridge-{index}.rs'
            source.write_text('pub mod compiler_mir {' + ''.join((valid | {key: replacement}).values()) + '}')
            bridge = self.base / f'libhooks{index}.rlib'
            result = self.command([str(self.rustc), str(source), '--edition=2024',
                '--crate-name', 'otelc_rust_bridge', '--crate-type', 'rlib', '-o', str(bridge)])
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            result, inventory, product = self.compile(entry, 'lifetime-weave', bridge=bridge,
                request_changes=requested)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'incompatible signature', result.stderr)
            self.assertNotIn(b'internal compiler error', result.stderr)
            self.assertFalse(product.exists()); self.assertFalse(inventory.exists())

    def test_automatic_drop_refuses_discovery_from_another_compiler_flag_profile(self) -> None:
        entry = self.source('flags.rs', 'struct Item(i32);impl Drop for Item{fn drop(&mut self){}}'
            'fn workload(value:i32)->i32{let item=Item(value*7);std::hint::black_box(&item);item.0}'
            'fn main(){std::hint::black_box(workload(3));}')
        requested = self.lifetime_request(entry, self.lifetime_policy(), optimisation=0)
        result, inventory, product = self.compile(entry, 'lifetime-weave', optimisation=3,
                                                   request_changes=requested)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'MIR body binding differs', result.stderr)
        self.assertFalse(product.exists()); self.assertFalse(inventory.exists())
