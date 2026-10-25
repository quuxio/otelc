"""Compile the exact telemetry extension against the real pinned baggage API."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from ci import build_go_upstream as tooling
from otelc import go_context_bundle as context
from otelc import go_telemetry_bundle as telemetry

ROOT = Path(__file__).resolve().parents[1]


class GoTelemetryBundleTests(unittest.TestCase):
    def test_changed_frame_and_clone_refuse_before_generating_a_bridge(self) -> None:
        from test_go_context_bundle import CLONE
        for source in (b'changed', CLONE.replace(b'&spanWrapper{last, nil, tc.sw.ended}', b'changed')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                telemetry.extend({telemetry.TRACE_OUTPUT + 'otel_trace_context.go': source})

    def test_exact_helper_has_compiled_coverage_and_real_baggage_clear_semantics(self) -> None:
        tools = tooling.build(os.environ.get('OTELC_GO', 'go'), ROOT / 'build/go-upstream')
        environment = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')
        with tempfile.TemporaryDirectory(prefix='go-telemetry-', dir=ROOT / 'build') as temporary:
            root = Path(temporary).resolve()
            project = root / 'fixture'
            shutil.copytree(ROOT / 'tests/fixtures/go-telemetry', project)
            # Only the TLS endpoint and linker registration differ. The helper
            # and official baggage presence query algorithms remain unchanged.
            module = 'go.opentelemetry.io/otel/otelc-telemetry-fixture'
            source = telemetry.SOURCE.removeprefix(b'//go:build ignore\n').replace(
                b'"runtime"', ('runtime "' + module + '/gls"').encode()).replace(
                b'go.opentelemetry.io/otelc/pkg/runtime.RegisterImplicitTelemetry',
                (module + '/bridge.RegisterImplicitTelemetry').encode())
            (project / 'helper.go').write_bytes(source)
            (project / 'bridge/bridge.go').write_bytes(telemetry.BRIDGE.replace(b'package runtime', b'package bridge', 1))
            download = subprocess.check_output([tools['go'], 'mod', 'download', '-json',
                                                'go.opentelemetry.io/otel@v1.47.0'],
                                               cwd=project, env=environment, timeout=180)
            official = json.loads(download)
            core = root / 'official-core'
            shutil.copytree(official['Dir'], core)
            query = core / 'internal/baggage/otelc_context.go'
            query.parent.chmod(0o700)
            query.write_bytes(telemetry.BAGGAGE.removeprefix(b'//go:build ignore\n'))
            subprocess.run([tools['go'], 'mod', 'edit', '-replace=go.opentelemetry.io/otel=' + str(core)],
                           cwd=project, env=environment, check=True, capture_output=True, timeout=60)
            subprocess.run([tools['go'], 'mod', 'tidy'], cwd=project, env=environment,
                           check=True, capture_output=True, timeout=180)
            profile = root / 'profile.out'
            result = subprocess.run([tools['go'], 'test', '-mod=readonly', '-race', '-count=1',
                                     '-coverpkg=./...,go.opentelemetry.io/otel/internal/baggage',
                                     '-coverprofile=' + str(profile), './...'], cwd=project,
                                    env=environment, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            shutil.copyfile(profile, ROOT / 'build/go-telemetry-component-coverage.out')
            components = {}
            for name in (module + '/helper.go', module + '/bridge/bridge.go',
                         'go.opentelemetry.io/otel/internal/baggage/otelc_context.go'):
                # -coverpkg emits a block for each test binary that links it.
                # Join identical positions; one binary's unused copy must not
                # erase execution witnessed by another binary.
                blocks = {}
                for line in profile.read_text().splitlines()[1:]:
                    position, count, hits = line.split()
                    if position.startswith(name + ':'):
                        if position in blocks:
                            self.assertEqual(blocks[position][0], int(count))
                        blocks[position] = (int(count), int(hits) + blocks.get(position, (0, 0))[1])
                self.assertTrue(blocks, 'generated helper has no compiled coverage: ' + name)
                total = sum(count for count, _ in blocks.values())
                covered = sum(count for count, hits in blocks.values() if hits)
                self.assertGreaterEqual(covered / total, .8, name)
                components[name] = {'covered_statements': covered, 'total_statements': total,
                                    'statement_percent': 100 * covered / total}
            receipt = {'generated_source_sha256': context.digest(telemetry.SOURCE),
                       'compiled_source_sha256': context.digest(source),
                       'components': components,
                       'scope': 'Exact metadata helper with fixture TLS/link endpoints and actual pinned baggage API; '
                                'native TLS, tasks and span hooks checked separately.'}
            (ROOT / 'build/go-telemetry-component-coverage.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
