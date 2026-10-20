"""One ordinary HTTP request, for the two-process tracing example."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer


def process_order(path: str) -> bytes:
    """Return a result or reject the example's deliberately invalid order."""
    if path == '/error':
        raise ValueError('invalid order')
    return b'{"result":42}\n'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        """Translate a business failure into an ordinary HTTP response."""
        try:
            body = process_order(self.path)
            status = 200
        except ValueError:
            body, status = b'{"error":"invalid order"}\n', 503
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Keep the example's process output deterministic."""


def main() -> None:
    """Serve one request and exit normally so telemetry can flush."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    with HTTPServer(('127.0.0.1', args.port), Handler) as server:
        print('ready', flush=True)
        server.handle_request()


if __name__ == '__main__':
    main()
