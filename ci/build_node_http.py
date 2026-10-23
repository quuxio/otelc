"""Build a source-hash-bound preservation patch around the pinned official HTTP instrumentor."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = {
    'http.js': '2e637ba16279c5fa1308ce3cee880cd1585e3f674dda94aeda012f6eaf6ffb96',
    'index.js': '1b985509c478058b744d35f2b9d07d71befc8fb403c221816f4f7402d08ed98f',
    'internal-types.js': '445c9cefb65d3ffdc4d1bbad9e1e3c1b56391ad3e0de94eaadcd9e4809bc53f6',
    'semconv.js': 'fff38414fd02e88d8dc3a4e1021a49d906e18d2c2eb22e356795b914b7d952ca',
    'types.js': 'b0d2bc4142d0c62d43f996aaeb64f22c4889ac853f8a3765758b505d972d0149',
    'utils.js': 'c90c200654a576866dfeb5bea380140ea9eec692b1e308fe6d4193344592b6e7',
    'version.js': 'cab38007ff5072eca021f9b940dca984ad59cd13368324d2186a9c8943e9fc4a',
}
IMPORT = 'const otelcPreservation = require("../../../../../adapters/node/src/http-preservation.ts");\n'


def replace_once(source: str, old: str, new: str) -> str:
    """Refuse an upstream shape change rather than applying an approximate patch."""
    if source.count(old) != 1:
        raise ValueError('pinned official HTTP patch context changed')
    return source.replace(old, new)


def build() -> Path:
    """Leave installed upstream files intact and publish an immutable private product."""
    upstream = ROOT / 'node_modules/@opentelemetry/instrumentation-http'
    if json.loads((upstream / 'package.json').read_text())['version'] != '0.223.0':
        raise ValueError('HTTP instrumentation must match the qualified 0.223.0 version')
    products: dict[str, bytes] = {}
    for name, expected in UPSTREAM.items():
        data = (upstream / 'build/src' / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('pinned official HTTP runtime source checksum mismatch: ' + name)
        products['build/src/' + name] = data
    http = products['build/src/http.js'].decode()
    start = http.index('            if (!optionsParsed.headers) {')
    end = http.index('            return api_1.context.with(requestContext, () => {', start)
    http = http[:start] + '            optionsParsed.headers = otelcPreservation.injectHeaders(requestContext, optionsParsed.headers);\n' + http[end:]
    if http.count('error.name') != 2:
        raise ValueError('pinned official HTTP error label shape changed')
    http = http.replace('error.name', 'otelcPreservation.errorType(error)')
    utils = products['build/src/utils.js'].decode()
    utils = replace_once(utils, '''    const message = error.message;
    span.setAttribute(semantic_conventions_1.ATTR_ERROR_TYPE, error.name);
    span.setStatus({ code: api_1.SpanStatusCode.ERROR, message });
    span.recordException(error);''', '''    span.setAttribute(semantic_conventions_1.ATTR_ERROR_TYPE, otelcPreservation.errorType(error));
    span.setStatus({ code: api_1.SpanStatusCode.ERROR });''')
    for name, source in (('http.js', http), ('utils.js', utils)):
        # The upstream maps describe the unpatched files; do not publish stale maps.
        source = replace_once(source, '"use strict";\n', '"use strict";\n' + IMPORT)
        products['build/src/' + name] = (source.split('//# sourceMappingURL=')[0].rstrip() + '\n').encode()
    products['LICENSE'] = (upstream / 'LICENSE').read_bytes()
    products['package.json'] = b'{"name":"@opentelemetry/instrumentation-http","version":"0.223.0-otelc.1","type":"commonjs"}\n'
    manifest = {'schema_version': 1, 'upstream_version': '0.223.0', 'patch_version': 1,
        'upstream_sha256': UPSTREAM,
        'helper_sha256': hashlib.sha256((ROOT / 'adapters/node/src/http-preservation.ts').read_bytes()).hexdigest(),
        'files': {name: hashlib.sha256(data).hexdigest() for name, data in products.items()}}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
    tools = ROOT / 'build/tools'
    tools.mkdir(parents=True, exist_ok=True)
    directory = tools / ('otelc-http-0.223.0-' + identity)
    if directory.exists():
        if sorted(path.relative_to(directory).as_posix() for path in directory.rglob('*') if path.is_file()) != sorted(products):
            raise ValueError('existing private HTTP product file set changed')
        if any((directory / name).read_bytes() != data for name, data in products.items()):
            raise ValueError('existing private HTTP product bytes changed')
    else:
        with tempfile.TemporaryDirectory(prefix='http-build-', dir=tools) as temporary:
            candidate = Path(temporary) / 'product'
            for name, data in products.items():
                output = candidate / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
            os.rename(candidate, directory)
    manifest['directory'] = directory.name
    with tempfile.NamedTemporaryFile(mode='w', dir=tools, delete=False) as output:
        json.dump(manifest, output, sort_keys=True)
        output.write('\n')
        temporary = Path(output.name)
    try:
        os.replace(temporary, tools / 'otelc-http.json')
    finally:
        temporary.unlink(missing_ok=True)
    print('Private official HTTP instrumentation: ' + directory.name)
    return directory


if __name__ == '__main__':
    build()
