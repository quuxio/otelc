"""Ordinary urllib and WSGI client/server; business output and request audit."""

import json
import os
import sys
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIRequestHandler, make_server


def workloadRequest(target, message):
    headers = {'X-Business': 'unchanged', 'X-Repeated': 'one,two'}
    original = dict(headers)
    request = Request(target + message['path'], headers=headers)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as failure:
        response = failure
    with response:
        return {'status': response.code, 'body': response.read().decode(),
                'unchanged': headers == original}


def workloadHandle(environ, start_response):
    path = environ['PATH_INFO']
    with Path(argument).open('a') as audit:
        audit.write(json.dumps({'path': path, 'headers': {
            name: value for name, value in environ.items() if name.startswith('HTTP_')}}) + '\n')
    body = (workloadRequest(downstream, {'path': '/sink'})['body'] if path == '/relay'
            else 'bad' if path == '/error' else '8' if environ.get('HTTP_X_BUSINESS') == 'unchanged' else 'missing-header')
    data = body.encode()
    start_response('500 Internal Server Error' if path == '/error' else '200 OK',
                   [('Content-Type', 'text/plain'), ('Content-Length', str(len(data)))])
    return [data]


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Requests are retained in the business audit instead of stderr logs.
        pass


role, argument, downstream = sys.argv[1:]
if role == 'server':
    with make_server('127.0.0.1', 0, workloadHandle, handler_class=QuietHandler) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        print(json.dumps({'port': server.server_port, 'pid': os.getpid()}), flush=True)
        try:
            for line in sys.stdin:
                if line.strip() == 'quit':
                    break
        finally:
            server.shutdown()
            worker.join()
elif role == 'client':
    print(json.dumps({'ready': True, 'pid': os.getpid()}), flush=True)
    for line in sys.stdin:
        if line.strip() == 'quit':
            break
        print(json.dumps(workloadRequest(argument, json.loads(line))), flush=True)
else:
    raise ValueError('expected server or client role')
