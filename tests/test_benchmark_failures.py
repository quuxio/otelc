"""Keep measured failures primary while every independent cleanup still runs."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from otelc import benchmark, native_benchmark, node_benchmark
from otelc.config import Policy

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkFailurePriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.policy = Policy.load(ROOT / 'examples/otelc.toml')

    def test_stop_attempts_reap_and_both_pipe_closes_after_failed_quit_and_kill(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        failure = BrokenPipeError('quit failed')
        process.stdin.write.side_effect = failure
        process.kill.side_effect = ProcessLookupError('already exited')
        process.wait.side_effect = TimeoutError('reap failed')
        process.stdin.close.side_effect = OSError('input close failed')
        process.stdout.close.side_effect = OSError('output close failed')
        with self.assertRaises(BrokenPipeError) as caught:
            benchmark.stop(process)
        self.assertIs(caught.exception, failure)
        process.wait.assert_called_once_with(timeout=5)
        process.stdin.close.assert_called_once(); process.stdout.close.assert_called_once()
        self.assertEqual(failure.__notes__, ['Benchmark forced stop also failed: ProcessLookupError',
            'Benchmark reap also failed: TimeoutError', 'Benchmark stdin close also failed: OSError',
            'Benchmark stdout close also failed: OSError'])

    def test_successful_java_measurement_still_fails_if_its_only_error_is_shutdown(self) -> None:
        failure = OSError('stop failed')
        process = mock.Mock(pid=123)
        with mock.patch.object(benchmark.java, 'command', return_value=['unused']), \
                mock.patch.object(benchmark.subprocess, 'Popen', return_value=process), \
                mock.patch.object(benchmark, 'batch', return_value={'checksum': 0}), \
                mock.patch.object(benchmark, 'measure_batches'), \
                mock.patch.object(benchmark, 'stop', side_effect=failure):
            with self.assertRaises(OSError) as caught:
                benchmark.measure_process(self.policy, self.output, self.output / 'runtime',
                    plain=True, expected=0, records=benchmark.Measurements())
        self.assertIs(caught.exception, failure)

    def test_failed_driver_export_still_closes_both_sdk_owners_without_hiding_the_error(self) -> None:
        failure = RuntimeError('driver export failed')
        exporter = mock.Mock()
        exporter.export.side_effect = failure
        exporter.shutdown.side_effect = OSError('exporter shutdown failed')
        provider = mock.Mock()
        provider.shutdown.side_effect = ValueError('provider shutdown failed')
        report = {'run_id': 'literal', 'recorded_at_unix_ns': 1, 'language': 'java',
            'samples_ns_per_operation': {'plain': [1]}, 'runtime': 'unit',
            'config': {'lifetimes': {'enabled': False}}, 'build_flags': [], 'process_samples': []}
        with mock.patch('opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter', return_value=exporter), \
                mock.patch('opentelemetry.sdk.metrics.export.InMemoryMetricReader'), \
                mock.patch('opentelemetry.sdk.metrics.MeterProvider', return_value=provider):
            with self.assertRaises(RuntimeError) as caught:
                benchmark.export_result(report, 'http://127.0.0.1:1')
        self.assertIs(caught.exception, failure)
        provider.shutdown.assert_called_once(); exporter.shutdown.assert_called_once()
        self.assertEqual(failure.__notes__, ['Benchmark driver provider shutdown also failed: ValueError',
            'Benchmark driver exporter shutdown also failed: OSError'])

    def test_java_body_and_cancellation_survive_failed_shutdown(self) -> None:
        for kind in (RuntimeError, KeyboardInterrupt):
            with self.subTest(kind=kind):
                failure = kind('measured failure')
                process = mock.Mock(pid=123)
                with mock.patch.object(benchmark.java, 'command', return_value=['unused']), \
                        mock.patch.object(benchmark.subprocess, 'Popen', return_value=process), \
                        mock.patch.object(benchmark, 'batch', side_effect=failure), \
                        mock.patch.object(benchmark, 'stop', side_effect=OSError('stop failed')) as stop:
                    with self.assertRaises(kind) as caught:
                        benchmark.measure_process(self.policy, self.output, self.output / 'runtime',
                            plain=True, expected=0, records=benchmark.Measurements())
                self.assertIs(caught.exception, failure)
                self.assertIn('Java benchmark cleanup also failed: OSError', failure.__notes__)
                stop.assert_called_once_with(process)

    def test_node_body_and_cancellation_survive_stop_drain_and_input_failures(self) -> None:
        runtime = self.output / 'node'; runtime.mkdir()
        (runtime / 'node-launch.json').write_text('{}')
        for kind in (RuntimeError, KeyboardInterrupt):
            with self.subTest(kind=kind):
                failure = kind('measured failure')
                process = mock.Mock(pid=123)
                with mock.patch.object(node_benchmark.subprocess, 'Popen', return_value=process), \
                        mock.patch.object(node_benchmark.control, 'wait_ready', return_value={'sdk_pid': 456}), \
                        mock.patch.object(node_benchmark.control, 'request'), \
                        mock.patch.object(benchmark, 'batch', side_effect=failure), \
                        mock.patch.object(benchmark, 'stop', side_effect=OSError('stop failed')) as stop, \
                        mock.patch.object(node_benchmark.node, 'wait_report', return_value={}) as report, \
                        mock.patch.object(node_benchmark, 'validate_drain', side_effect=ValueError('drain failed')), \
                        mock.patch.object(node_benchmark.node_provenance, 'finish', return_value=False) as inputs:
                    with self.assertRaises(kind) as caught:
                        node_benchmark.measure_process(self.policy, self.output, runtime, ['unused'],
                            'instrumented', 0, benchmark.Measurements())
                self.assertIs(caught.exception, failure)
                self.assertIn('Node benchmark cleanup also failed: OSError', failure.__notes__)
                self.assertIn('Node benchmark SDK drain also failed: ValueError', failure.__notes__)
                self.assertIn('Node benchmark input verification also failed: ValueError', failure.__notes__)
                stop.assert_called_once(); report.assert_called_once(); inputs.assert_called_once()

    def test_native_body_and_cancellation_survive_invalid_final_sdk_report(self) -> None:
        process = SimpleNamespace(pid=123, supervisor_pid=456, sdk_report={})

        @contextmanager
        def application(*args, **kwargs):
            yield process

        for kind in (RuntimeError, KeyboardInterrupt):
            with self.subTest(kind=kind):
                failure = kind('measured failure')
                with mock.patch.object(native_benchmark.launch, 'application', application), \
                        mock.patch.object(native_benchmark.control, 'wait_ready'), \
                        mock.patch.object(native_benchmark.control, 'request'), \
                        mock.patch.object(benchmark, 'batch', side_effect=failure), \
                        mock.patch.object(native_benchmark, 'validate_drain', side_effect=ValueError('bad drain')) as drain, \
                        mock.patch.object(native_benchmark.launch, 'retain') as retain:
                    with self.assertRaises(kind) as caught:
                        native_benchmark.measure(self.policy, mock.Mock(), self.output, plain=False,
                            records=benchmark.Measurements(), expected=0)
                self.assertIs(caught.exception, failure)
                self.assertIn('Native benchmark final validation also failed: ValueError', failure.__notes__)
                drain.assert_called_once(); retain.assert_not_called()

    def test_native_owner_keeps_the_body_error_and_attempts_stop_completion_and_recheck(self) -> None:
        runtime = self.output / 'native-owner'
        owner = mock.Mock(pid=456)
        built = mock.Mock(directory=self.output / 'compiled')
        built.manifest.return_value = {'root': str(self.output)}
        failure = RuntimeError('measured failure')
        final = {'sdk_health': {'retained': True}, 'failures': {'source': 'changed'}, 'inputs_rechecked': False}
        launch = native_benchmark.launch
        with mock.patch.object(launch.native_launch, 'verify', side_effect=[None, ValueError('build changed')]) as verify, \
                mock.patch.object(launch.prepared, 'prepare', side_effect=lambda *args, **kwargs: runtime.mkdir(mode=0o700)), \
                mock.patch.object(launch.prepared, 'command', return_value=['unused']), \
                mock.patch.object(launch.subprocess, 'Popen', return_value=owner), \
                mock.patch.object(launch, 'start', return_value=123), \
                mock.patch.object(benchmark, 'stop', side_effect=OSError('stop failed')) as stop, \
                mock.patch.object(launch.prepared, 'execution', return_value=final) as execution:
            with self.assertRaises(RuntimeError) as caught:
                with launch.application(self.policy, built, runtime, plain=False) as process:
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(process.sdk_report, final['sdk_health'])
        self.assertEqual(failure.__notes__, ['Native benchmark stop also failed: OSError',
            'Native benchmark completion also failed: ValueError', 'Native benchmark build verification also failed: ValueError'])
        stop.assert_called_once(); execution.assert_called_once(); self.assertEqual(verify.call_count, 2)


if __name__ == '__main__':
    unittest.main()
