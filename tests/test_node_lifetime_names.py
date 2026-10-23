"""Lifetime owner probes preserve application names, brands and public properties."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import control, node, node_recipe
from otelc.config import Policy
from test_java_integration import Capture, batch
from test_node_source import policy_for


APPLICATION = """const rows = [];
let getterCalls = 0;
let proxyHooks = 0;
let failRelease = false;
const marker = new Error('original release');
function factory() {
  return class Item {
    #_otelc_owner = 11;
    static early = (() => {
      const item = new this();
      item.close();
      return { item, later: this.later ?? 'unset' };
    })();
    static later = 'ready';
    close() {
      function Item() { return value; }
      var Item;
      const value = 7;
      rows.push({ kind: 'function', name: Item.name, value: Item() });
      if (failRelease) throw marker;
    }
    dispose() {
      const Item = class { static seen = this.name; };
      rows.push({ kind: 'class', name: Item.name, seen: Item.seen });
    }
    other() {
      const Item = class {
        static get name() { getterCalls++; return 'custom'; }
        static seen = this.name;
      };
      rows.push({ kind: 'custom', name: Item.name, seen: Item.seen });
    }
  };
}
const Left = factory();
const Right = factory();
const left = new Left();
const right = new Right();
const proxy = new Proxy({}, {
  get() { proxyHooks++; throw new Error('unexpected get'); },
  has() { proxyHooks++; throw new Error('unexpected has'); },
  getPrototypeOf() { proxyHooks++; throw new Error('unexpected prototype read'); }
});
Left.prototype.close.call(right);
for (const receiver of [7, null, undefined, Symbol('receiver'), function Receiver() {}, proxy]) {
  Left.prototype.close.call(receiver);
}
let borrowedFailure = false;
let ownFailure = false;
failRelease = true;
try { Left.prototype.close.call(right); } catch (error) { borrowedFailure = error === marker; }
try { left.close(); } catch (error) { ownFailure = error === marker; }
failRelease = false;
const input = createInterface({ input: process.stdin, terminal: false });
input.on('line', command => {
  if (command === 'inspect') {
    console.log(JSON.stringify({ phase: 'ready', early: [Left.early.later, Right.early.later],
      later: [Left.later, Right.later], borrowedFailure, ownFailure }));
  } else if (command === 'finish') {
    left.close(); left.close(); right.dispose(); right.other();
    console.log(JSON.stringify({ rows, getterCalls, proxyHooks,
      own: Reflect.ownKeys(left), prototype: Reflect.ownKeys(Left.prototype),
      classKeys: Reflect.ownKeys(Left), names: [Left.name, Right.name] }));
  } else if (command === 'quit') {
    input.close(); process.stdin.pause();
  }
});
"""

PROFILES = [('javascript', 'native', 'cjs'), ('javascript', 'native', 'mjs'),
            ('typescript', 'native', 'cts'), ('typescript', 'api', 'mts')]


class LifetimeNameTests(unittest.TestCase):
    def setUp(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(policy_for(capture).data)
        data['sources'] = {'include': ['**'], 'exclude': []}
        data['functions'] = {'include': [], 'exclude': []}
        data['lifetimes'].update(include=['*Item'], release_methods=['close', 'dispose', 'other'])
        data['metrics'].update(collection=True, emission=False)
        data['traces']['enabled'] = False
        self.policy = Policy.parse(data)

    def test_shadowed_class_bindings_keep_names_and_borrowed_receiver_behaviour(self) -> None:
        for language, backend, extension in PROFILES:
            with self.subTest(language=language, backend=backend, extension=extension):
                self.exercise_profile(self.policy, language, backend, extension)

    def test_owner_probe_does_not_shadow_an_outer_private_name(self) -> None:
        source = """class Scope {
  #_otelc_owner2;
  make() {
    return class Item {
      #_otelc_owner;
      close() { const Item = 7; this.result = { value: Item, outer: #_otelc_owner2 in this }; }
    };
  }
}
const Generated = new Scope().make();
const item = new Generated(); item.close();
console.log(JSON.stringify(item.result));
"""
        for language, backend, extension in PROFILES:
            with self.subTest(language=language, backend=backend), tempfile.TemporaryDirectory(dir='/tmp') as temporary:
                root = Path(temporary).resolve()
                entry = root / ('app.' + extension)
                entry.write_text(source)
                for plain in (True, False):
                    runtime = root / ('plain' if plain else 'instrumented')
                    argv = node.command(self.policy, runtime, language, root=root, entry=entry, backend=backend, plain=plain)
                    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
                    if not plain:
                        report = node.wait_report(runtime)
                        self.assertEqual(report['lifetimes']['resource_completed'], 1)
                        self.assertEqual(report['lifetime_lost'], 0)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {'value': 7, 'outer': False})
                    self.assertEqual(entry.read_text(), source)

    def test_generated_owner_probe_is_not_an_application_function_under_wildcard_selection(self) -> None:
        data = copy.deepcopy(self.policy.data)
        data['functions']['include'] = ['*']
        root = Path(__file__).resolve().parent.parent
        transform = (root / 'adapters/node/src/compiler/transform.mjs').as_uri()
        script = """import {readFileSync} from 'node:fs';
import {transform} from TRANSFORM;
const plan=JSON.parse(readFileSync(0,'utf8'));
const registered=[];
const output=transform('class Item {close(){function Item(){return 7;} console.log(Item.name);}}',
  'app.cjs','app',plan,{register(name){registered.push(name);return true;}});
console.log(JSON.stringify({names:output.functions.map(row=>row.name),registered}));
""".replace('TRANSFORM', json.dumps(transform))
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            plan = node.prepare(Policy.parse(data), Path(temporary), 'javascript', root=root)
            result = subprocess.run([node.executable(), '--input-type=module', '-e', script],
                input=json.dumps(plan), capture_output=True, text=True, timeout=10, env=node_recipe.tooling_environment())
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        expected = ['app.Item.close.Item', 'app.Item.close']
        self.assertEqual(observed, {'names': expected, 'registered': expected})

    def exercise_profile(self, policy: Policy, language: str, backend: str, extension: str) -> None:
        prefix = ("import { createInterface } from 'node:readline';\n" if extension in ('mjs', 'mts') else
                  "const { createInterface } = require('node:readline');\n")
        expected = {'rows': [{'kind': 'function', 'name': 'Item', 'value': 7}] * 13 + [
            {'kind': 'class', 'name': 'Item', 'seen': 'Item'}, {'kind': 'custom', 'name': 'custom', 'seen': 'custom'}],
            'getterCalls': 2, 'proxyHooks': 0, 'own': [], 'prototype': ['constructor', 'close', 'dispose', 'other'],
            'classKeys': ['length', 'name', 'prototype', 'early', 'later'], 'names': ['Item', 'Item']}
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            root = Path(temporary).resolve()
            entry = root / ('app.' + extension)
            entry.write_text(prefix + APPLICATION)
            original = entry.read_bytes()
            for plain in (True, False):
                runtime = root / ('plain' if plain else 'instrumented')
                argv = node.command(policy, runtime, language, root=root, entry=entry, backend=backend, plain=plain)
                with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                    try:
                        ready = batch(process, 'inspect')
                        if not plain:
                            control.wait_ready(runtime / 'control.json', process.pid)
                            self.assertEqual(control.request(runtime / 'control.json', 'status')['lifetimes']['resource_completed'], 2)
                        observed = batch(process, 'finish')
                        stdout, stderr = process.communicate('quit\n', timeout=20)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                        if not plain:
                            report = node.wait_report(runtime)
                self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                self.assertEqual(ready, {'phase': 'ready', 'early': ['unset', 'unset'], 'later': ['ready', 'ready'],
                                         'borrowedFailure': True, 'ownFailure': True})
                self.assertEqual(observed, expected)
                self.assertEqual(entry.read_bytes(), original)
                if not plain:
                    self.assertEqual(report['phase'], 'closed')
                    self.assertEqual(report['function_completions'], 0)
                    self.assertEqual(report['native_callback_failures'], 0)
                    self.assertTrue(all(value == 0 for value in report['runtime_losses'].values()))
                    self.assertEqual(report['lifetimes']['admitted'], 4)
                    self.assertEqual(report['lifetimes']['resource_completed'], 4)
                    self.assertEqual(report['lifetimes']['failed_releases'], 1)
                    self.assertEqual(report['lifetime_lost'], 0)
