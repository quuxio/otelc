"""Replace actual validated bindings with a FIFO before metadata publication."""

from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from otelc import java, prepared_java, witness
from otelc.config import Policy
from test_prepared_java import binding
from test_witness import SOURCE


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    source = root / 'Harness.java'
    source.write_text(SOURCE)
    classes = root / 'classes'
    subprocess.run([java.executable('javac'), '-d', str(classes), str(source)], check=True)
    manifest = root / 'application.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'main_class': 'Harness', 'classpath': ['classes'],
        'bindings': {owner: binding(source, classes / (owner + '.class'), 'src/Harness.java')
                     for owner in ('Harness', 'Work')}}))
    application = prepared_java.PreparedApplication.load(manifest)
    declarations = {role: {'owner': 'Work' if role == 'target' else 'Harness', 'name': role,
        'descriptor': '()V' if role == 'rendezvous' else '(I)I'} for role in witness.ROLES}
    bindings = root / 'bindings.json'
    witness.compile_bindings(application, declarations, bindings)
    original_digest = hashlib.sha256(bindings.read_bytes()).hexdigest()
    policy = Policy.parse({'schema_version': 3, 'languages': ['java'], 'sources': {'include': ['src/**']},
        'functions': {'include': ['Work.target(*)']}, 'traces': {'enabled': True, 'sample_ratio': 1.0}})
    original_identity = prepared_java.file_identity
    replaced = []

    def replace_after_identity(path: Path) -> dict:
        identity = original_identity(path)
        if path == bindings and not replaced:
            bindings.unlink()
            os.mkfifo(bindings, 0o600)
            replaced.append(True)
            print('validated-binding-replaced-with-fifo', flush=True)
        return identity

    runtime = root / 'runtime'
    runtime.mkdir(mode=0o700)
    launch = None
    with patch.object(prepared_java, 'file_identity', side_effect=replace_after_identity):
        try:
            launch = prepared_java.prepare(application, policy, runtime, witness_bindings=bindings)
        except ValueError:
            pass
    assert replaced
    if launch is not None:
        metadata = json.loads((runtime / 'witness.json').read_text())
        assert metadata['bindings_sha256'] == original_digest
        try:
            launch.verify_inputs()
        except ValueError:
            pass
        else:
            raise AssertionError('changed input was accepted on recheck')
    assert source.read_text() == SOURCE
    print('source-change-refused-without-application-execution', flush=True)


if __name__ == '__main__':
    main()
