"""An ordinary standard-library HTTP client with no instrumentation imports."""
import argparse
import json
from http.client import HTTPConnection


def request_order(port: int, path: str) -> dict[str, object]:
    """Read the complete response, preserving the server's application result."""
    connection = HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('GET', path)
        with connection.getresponse() as response:
            return {'status': response.status, 'body': json.loads(response.read())}
    finally:
        connection.close()


def main() -> None:
    """Call the example server once."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--path', choices=('/', '/error'), default='/')
    args = parser.parse_args()
    print(json.dumps(request_order(args.port, args.path), sort_keys=True))


if __name__ == '__main__':
    main()
