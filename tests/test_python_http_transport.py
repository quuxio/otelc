"""Real exporters must not recursively trace their own configured HTTP sink."""
import http.client
import unittest

from test_python_spans import Capture, Monitor, ROOT, Runtime, receiver, trace_plan
from quux_otelc_python.deadlines import CURRENT


class PythonHTTPTelemetryTests(unittest.TestCase):
    def test_real_trace_export_does_not_create_client_or_receiver_spans(self):
        with receiver() as (endpoint, requests):
            policy = trace_plan()
            policy['propagation'] = {'http': True, 'tasks': False}
            policy['trace_export']['endpoint'] = endpoint + '/v1/traces'
            policy['metrics_endpoint'] = endpoint + '/v1/metrics'
            runtime = Runtime(policy, Capture())
            self.addCleanup(runtime.close)
            monitor = Monitor(policy, runtime)
            self.addCleanup(monitor.close)
            monitor.install()
            application = {}
            exec(compile('def work():\n    return 7\n', str(ROOT / 'trace_fixture.py'), 'exec'), application)
            self.assertEqual(application['work'](), 7)
            runtime.traces.flush()
            monitor.close()
            report = runtime.close()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], '/v1/traces')
            self.assertEqual(report['traces']['completed_trees'], 1)
            self.assertFalse(report['traces']['queued_trees'])
            self.assertFalse(report['traces']['losses'])
            self.assertFalse(report['export_loss'])

    def test_an_application_request_with_another_authority_remains_traced(self):
        with receiver() as (endpoint, requests):
            policy = trace_plan()
            policy['propagation'] = {'http': True, 'tasks': False}
            policy['trace_export']['endpoint'] = endpoint + '/v1/traces'
            runtime = Runtime(policy, Capture())
            self.addCleanup(runtime.close)
            monitor = Monitor(policy, runtime)
            self.addCleanup(monitor.close)
            monitor.install()
            port = int(endpoint.rsplit(':', 1)[1])
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            connection.request('POST', '/v1/traces', headers={'Host': f'application.invalid:{port}'})
            self.assertEqual(connection.getresponse().read(), b'')
            connection.close()
            runtime.traces.flush()
            monitor.close()
            report = runtime.close()
            self.assertEqual(report['traces']['completed_trees'], 2)
            self.assertFalse(report['traces']['losses'])
            self.assertEqual(len(requests), 3)

    def test_own_transport_marker_and_hostless_signal_endpoints_are_suppressed(self):
        with receiver() as (endpoint, requests):
            policy = trace_plan()
            policy['propagation'] = {'http': True, 'tasks': False}
            policy['trace_export']['endpoint'] = endpoint + '/v1/traces'
            policy['metrics_endpoint'] = endpoint.replace('127.0.0.1', 'localhost') + '/v1/metrics'
            runtime = Runtime(policy, Capture())
            self.addCleanup(runtime.close)
            monitor = Monitor(policy, runtime)
            self.addCleanup(monitor.close)
            monitor.install()
            port = int(endpoint.rsplit(':', 1)[1])
            for path in ('/v1/traces', '/v1/metrics'):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                token = CURRENT.set(object())
                try:
                    connection.putrequest('POST', path, skip_host=True)
                    connection.putheader('Content-Length', '0')
                    connection.endheaders()
                    self.assertEqual(connection.getresponse().read(), b'')
                finally:
                    CURRENT.reset(token)
                    connection.close()
            monitor.close()
            report = runtime.close()
            self.assertEqual(len(requests), 2)
            self.assertTrue(all('traceparent' not in {key.lower() for key in item[1]} for item in requests))
            self.assertEqual(report['traces']['completed_trees'], 0)
            self.assertFalse(report['traces']['losses'])
