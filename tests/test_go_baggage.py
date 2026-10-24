"""Unedited services inherit telemetry metadata through functions and tasks."""
from __future__ import annotations

import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.request

from ci import build_go_upstream as tooling
from otelc import control, go_build
from otelc.config import Policy
import test_go_http_integration as http_fixture
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


class GoBaggageTests(unittest.TestCase):
    # Reuse the actual owned-process protocol, not the HTTP test cases.
    start = http_fixture.GoHTTPIntegrationTests.start
    line = http_fixture.GoHTTPIntegrationTests.line
    cleanup = http_fixture.GoHTTPIntegrationTests.cleanup
    finish = http_fixture.GoHTTPIntegrationTests.finish
    settings = http_fixture.GoHTTPIntegrationTests.settings

    def test_implicit_baggage_isolated_across_requests_tasks_and_live_gates(self) -> None:
        tools = tooling.build('go', ROOT / 'build/go-upstream')
        source = ROOT / 'tests/fixtures/go-baggage'
        before = go_build.inputs(source)
        with tempfile.TemporaryDirectory(prefix='go-baggage-', dir=ROOT / 'build') as temporary:
            self.base = Path(temporary).resolve()
            self.capture = Capture()
            self.addCleanup(self.capture.close)
            self.processes = []
            self.addCleanup(self.cleanup)
            self.sequence = 0
            for tasks in (False, True):
                data = copy.deepcopy(Policy.load(ROOT / 'examples/http-context.toml').data)
                data['context']['tasks'] = tasks
                self.policy = Policy.parse(data)
                self.build = self.base / ('build-' + str(tasks))
                go_build.build_project(source, '.', self.policy, self.build, tools)
                for traces in (True, False):
                    server, sr, ready = self.start('server', self.settings('baggage-server', traces=traces))
                    relay, rr, front = self.start('relay', self.settings('baggage-relay', traces=traces), ready['url'])
                    pids = [control.request(runtime / 'control.json', 'status')['pid'] for runtime in (sr, rr)]

                    def request(index: int, *, child: bool) -> dict:
                        headers = {'traceparent': f'00-{index + 1:032x}-2222222222222222-01',
                                   'baggage': f'tenant=case{index},task=abc'}
                        url = front['url'] + ('?child=1' if child else '')
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
                            return json.load(response)

                    for flags in ((False, False), (True, False), (False, True), (True, True)):
                        for runtime, pid in zip((sr, rr), pids):
                            control.request(runtime / 'control.json', 'collection ' + ('on' if flags[0] else 'off'))
                            state = control.request(runtime / 'control.json', 'emission ' + ('on' if flags[1] else 'off'))
                            self.assertEqual(state['pid'], pid)
                        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                            answers = list(pool.map(lambda i: request(i, child=tasks), range(8)))
                        for index, answer in enumerate(answers):
                            self.assertEqual(answer['value'], '42')
                            self.assertEqual(answer['incoming_baggage'], f'tenant=case{index},task=abc')
                            self.assertEqual(set(answer['baggage'].split(',')), {f'tenant=case{index}', 'task=abc'})
                            self.assertEqual(answer['traceparent'].split('-')[1], f'{index + 1:032x}')
                    for owner, runtime in ((relay, rr), (server, sr)):
                        self.assertEqual(self.finish(owner, runtime)['failures'], {})
            self.assertEqual(go_build.inputs(source), before)


if __name__ == '__main__':
    unittest.main()
