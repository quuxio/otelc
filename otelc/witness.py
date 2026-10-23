"""Explicit Java invocation diagnostics; normal launch and health schemas stay unchanged."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from otelc import file_inputs, java, java_helpers

if TYPE_CHECKING:
    from otelc.config import Policy
    from otelc.prepared_java import PreparedApplication

PROFILE = "java-sync-action-v1"
ROLES = ("envelope", "rendezvous", "wrapper", "target")
FIELDS = {"owner", "name", "descriptor", "class_sha256", "method_sha256", "probe_id"}
REPLY_FIELDS = {"schema_version", "profile", "runtime_id", "request_id", "error", "build_sha256",
                "selection_sha256", "policy_sha256", "tooling_sha256", "wrapper_probe_id",
                "envelope_probe_id", "rendezvous_probe_id", "target_probe_id",
                "rejected_probes", "max_scopes", "max_spans_per_scope", "scope"}
SCOPE_FIELDS = {"cookie", "sequence", "scope_id", "case_id", "attempt_id", "phase", "failure",
                "trace_id", "root_span_id", "target_span_id", "root_exited", "envelope_exited",
                "target_exited", "target_entries", "export_failures", "flush_failures", "checkpoint",
                "producer_complete", "receiver_confirmation_required", "started_ids", "ended_ids",
                "exported_ids", "pending_exports", "queue_dropped"}


def hexadecimal(value: object, size: int, *, nullable: bool = False) -> bool:
    """Check an opaque protocol identity without coercing a number or missing value."""
    return (nullable and value is None) or isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{size}}}", value) is not None


def decimal(value: object) -> bool:
    """Keep counters exact and bounded across language JSON implementations."""
    return isinstance(value, str) and re.fullmatch(r"0|[1-9]\d{0,18}", value, re.ASCII) is not None


def validate_reply(result: object, request_id: str | None) -> dict[str, Any]:
    """Refuse unknown wire shapes or a completion claim unsupported by its own evidence."""
    if (not isinstance(result, dict) or set(result) != REPLY_FIELDS
            or type(result["schema_version"]) is not int or result["schema_version"] != 1
            or result["profile"] != PROFILE or result["request_id"] != request_id
            or not hexadecimal(result["runtime_id"], 32)
            or not decimal(result["rejected_probes"])
            or type(result["max_scopes"]) is not int or result["max_scopes"] != 64
            or type(result["max_spans_per_scope"]) is not int or result["max_spans_per_scope"] != 1024
            or any(not hexadecimal(result[key], 64) for key in
                   ("build_sha256", "selection_sha256", "policy_sha256", "tooling_sha256",
                    "wrapper_probe_id", "envelope_probe_id", "rendezvous_probe_id", "target_probe_id"))
            or result["error"] is not None and (not isinstance(result["error"], str)
                or not re.fullmatch(r"[a-z][a-z_]{0,63}", result["error"]))):
        raise ValueError("invalid or uncorrelated witness response")
    scope = result["scope"]
    if scope is not None:
        _validate_scope_identity(scope)
        _validate_completion(scope, _span_sets(scope))
    return result


def _validate_scope_identity(scope: object) -> None:
    if (not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS
            or not hexadecimal(scope["cookie"], 32) or not decimal(scope["sequence"])
            or scope["sequence"] == "0"
            or not isinstance(scope["phase"], str)
            or scope["phase"] not in {"entered", "ready", "armed", "consumed", "exited", "expired",
                                      "cancelled", "complete", "invalidated", "incomplete", "terminated"}
            or any(not decimal(scope[key]) for key in
                   ("target_entries", "export_failures", "flush_failures", "pending_exports"))
            or any(type(scope[key]) is not bool for key in ("root_exited", "envelope_exited", "target_exited",
                    "checkpoint", "producer_complete", "receiver_confirmation_required"))
            or scope["receiver_confirmation_required"] is not True):
        raise ValueError("invalid witness scope evidence")
    for key, size in (("scope_id", 32), ("case_id", 64), ("attempt_id", 64), ("trace_id", 32),
                      ("root_span_id", 16), ("target_span_id", 16)):
        if not hexadecimal(scope[key], size, nullable=True):
            raise ValueError("invalid witness scope identity")
    if scope["failure"] is not None and (not isinstance(scope["failure"], str)
            or not re.fullmatch(r"[a-z][a-z_]{0,63}", scope["failure"])):
        raise ValueError("invalid witness scope failure")
    if scope["queue_dropped"] is not None and not decimal(scope["queue_dropped"]):
        raise ValueError("invalid witness queue evidence")


def _span_sets(scope: dict[str, Any]) -> list[set[str]]:
    ids: list[set[str]] = []
    for key in ("started_ids", "ended_ids", "exported_ids"):
        values = scope[key]
        if (not isinstance(values, list) or len(values) > 1024
                or any(not hexadecimal(value, 16) for value in values) or len(set(values)) != len(values)):
            raise ValueError("invalid witness span IDs")
        ids.append(set(values))
    if not ids[1].issubset(ids[0]) or not ids[2].issubset(ids[0]):
        raise ValueError("witness span accounting is inconsistent")
    return ids


def _validate_completion(scope: dict[str, Any], ids: list[set[str]]) -> None:
    if scope["producer_complete"] and (scope["phase"] != "complete" or scope["failure"] is not None
            or not ids[0] or ids[0] != ids[1] or ids[0] != ids[2]
            or any(scope[key] is not True for key in ("checkpoint", "root_exited", "envelope_exited", "target_exited"))
            or scope["pending_exports"] != "0" or scope["queue_dropped"] not in (None, "0") or scope["target_entries"] == "0"
            or scope["root_span_id"] not in ids[0] or scope["target_span_id"] not in ids[0]
            or any(scope[key] is None for key in ("scope_id", "case_id", "attempt_id", "trace_id"))):
        raise ValueError("witness completion claim lacks matching evidence")


def _read_private_report(path: Path) -> bytes:
    if path.name != "witness-health.json":
        raise ValueError("expected witness-health.json")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("witness directory must be owned and private")
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("witness final report cannot be a symlink") from error
            raise
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077 or metadata.st_size > 4194304):
                raise ValueError("invalid private witness final report")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(4194305)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    if len(data) > 4194304:
        raise ValueError("oversized witness final report")
    return data


def read_final(path: Path) -> dict[str, Any]:
    """Read bounded private post-exit evidence, without inferring downstream delivery."""
    from otelc.prepared_java import unique_object

    result = json.loads(_read_private_report(path), object_pairs_hook=unique_object)
    fields = {"schema_version", "kind", "phase", "pid", "producer", "queue_evidence", "scopes"}
    if (not isinstance(result, dict) or set(result) != fields or type(result["schema_version"]) is not int
            or result["schema_version"] != 1 or result["kind"] != "otelc.java-witness-final"
            or result["phase"] != "final" or type(result["pid"]) is not int or result["pid"] <= 0
            or result["queue_evidence"] not in ("derived_after_shutdown", "unavailable")
            or not isinstance(result["scopes"], list) or len(result["scopes"]) > 64):
        raise ValueError("invalid witness final identity")
    validate_reply(result["producer"], None)
    if result["producer"]["scope"] is not None:
        raise ValueError("final producer header cannot hide a scope")
    _validate_final_contexts(result)
    return result


def _validate_final_contexts(result: dict[str, Any]) -> None:
    cookies = set()
    scope_ids = set()
    previous_sequence = 0
    for scope in result["scopes"]:
        if not isinstance(scope, dict):
            raise ValueError("invalid witness final scope")
        validate_reply(result["producer"] | {"scope": scope}, None)
        if scope["cookie"] in cookies or scope["scope_id"] is not None and scope["scope_id"] in scope_ids:
            raise ValueError("duplicate witness final context")
        cookies.add(scope["cookie"])
        scope_ids.add(scope["scope_id"])
        if int(scope["sequence"]) <= previous_sequence:
            raise ValueError("witness final contexts are not ordered")
        previous_sequence = int(scope["sequence"])
        if result["queue_evidence"] == "unavailable" and scope["queue_dropped"] is not None:
            raise ValueError("witness final queue claim lacks a supported drain")


def compile_bindings(application: PreparedApplication,
                     declarations: dict[str, dict[str, str]], output: Path) -> dict[str, Any]:
    """Compile frozen bindings offline; application classes are read, never executed."""
    if not isinstance(declarations, dict) or set(declarations) != set(ROLES):
        raise ValueError("exactly four witness role declarations are required")
    extension = java.ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar"
    compiler = java_helpers.tool(extension)
    if any(not path.is_file() for path in (extension, compiler)):
        raise ValueError("build the offline compiler with make java-build")
    before = application.input_identity()
    with tempfile.TemporaryDirectory(prefix="otelc-witness-") as directory:
        command = [java.executable("java"), "-cp", os.pathsep.join(map(str, (extension, compiler))),
                   "io.quux.otelc.witness.WitnessBindings"]
        for index, role in enumerate(ROLES):
            declaration = declarations[role]
            if (not isinstance(declaration, dict) or set(declaration) != {"owner", "name", "descriptor"}
                    or any(not isinstance(value, str) or not value for value in declaration.values())
                    or declaration["owner"].replace("/", ".") not in application.bindings):
                raise ValueError("witness roles must refer to declared original application classes")
            owner = declaration["owner"]
            raw = class_bytes(application, owner, identity=before)
            copy = Path(directory) / f"role-{index}.class"
            copy.write_bytes(raw)
            command.extend([str(copy), owner, declaration["name"], declaration["descriptor"]])
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    if application.input_identity() != before:
        raise ValueError("prepared inputs changed while compiling witness bindings")
    with output.open("x") as writer:
        writer.write(result.stdout)
    return load_bindings(output, application)


def _class_location(application: PreparedApplication, owner: str,
                    identity: dict[str, Any]) -> tuple[Path, str]:
    """Use the selected JDK's effective member, including versioned-only classes."""
    if owner.replace("/", ".") not in application.bindings:
        raise ValueError("witness class requires a source binding")
    name = owner + ".class"
    resolution = identity.get("archive_resolution")
    archives = {item["classpath_index"]: item for item in resolution["archives"]} if resolution else {}
    for index, entry in enumerate(application.classpath):
        if entry.is_dir() and (entry / name).is_file():
            return entry, name
        archive = archives.get(index)
        if archive is not None and owner.replace("/", ".") in archive["classes"]:
            return entry, archive["classes"][owner.replace("/", ".")]["entry"]
    raise ValueError("witness owner is absent from the explicit classpath")


def class_bytes(application: PreparedApplication, owner: str, *,
                identity: dict[str, Any] | None = None) -> bytes:
    """Read and check the actual runtime-selected original class definition."""
    current = application.input_identity() if identity is None else identity
    entry, member = _class_location(application, owner, current)
    if entry.is_dir():
        raw = (entry / member).read_bytes()
    else:
        with zipfile.ZipFile(entry) as archive:
            raw = archive.read(member)
    if hashlib.sha256(raw).hexdigest() != application.bindings[owner.replace("/", ".")]["class_sha256"]:
        raise ValueError("witness runtime class digest disagrees with prepared inputs")
    return raw


def digest(value: object) -> str:
    """Hash deterministic metadata independently of the application runtime."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_bindings(path: Path, application: PreparedApplication) -> dict[str, Any]:
    """Read frozen offline bindings and match every role to original application inputs."""
    return _binding_snapshot(path, application)[0]


def _binding_snapshot(path: Path, application: PreparedApplication
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate bytes and retain their opened-file identity without reopening."""
    from otelc.prepared_java import unique_object

    identity, content = file_inputs.checked_file(path, include_content=True, max_bytes=32768, include_stat=True)
    document = json.loads(content.decode('utf-8'), object_pairs_hook=unique_object)
    if (not isinstance(document, dict)
            or set(document) != {"schema_version", "kind", "profile", "bindings"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["kind"] != "otelc.java-witness-bindings"
            or document["profile"] != PROFILE
            or not isinstance(document["bindings"], dict)
            or set(document["bindings"]) != set(ROLES)):
        raise ValueError("expected complete Java witness bindings schema 1")
    for role, binding in document["bindings"].items():
        _validate_binding(role, binding, application)
    binding_identity = {"path": str(path), **{name: identity[name] for name in
                        ("sha256", "size", "inode", "device", "mtime_ns")}}
    return document, binding_identity


def _validate_binding(role: str, binding: object, application: PreparedApplication) -> None:
    if (not isinstance(binding, dict) or set(binding) != FIELDS
            or any(not isinstance(value, str) or not value or "\n" in value
                   for value in binding.values())):
        raise ValueError("invalid witness method binding")
    owner = binding["owner"].replace("/", ".")
    if owner not in application.bindings:
        raise ValueError("every witness owner requires an original source binding")
    if binding["class_sha256"] != application.bindings[owner]["class_sha256"]:
        raise ValueError("witness class digest disagrees with prepared inputs")
    if any(not re.fullmatch(r"[0-9a-f]{64}", binding[key])
           for key in ("class_sha256", "method_sha256", "probe_id")):
        raise ValueError("invalid witness digest")
    fields = [PROFILE, role, binding["owner"], binding["name"], binding["descriptor"],
              binding["class_sha256"], binding["method_sha256"]]
    if hashlib.sha256("\n".join(fields).encode()).hexdigest() != binding["probe_id"]:
        raise ValueError("witness probe ID disagrees with binding")


def origin(application: PreparedApplication, owner: str, *,
           identity: dict[str, Any] | None = None) -> str:
    """Use the same real file URL as the system classloader, without URL equality/DNS."""
    current = application.input_identity() if identity is None else identity
    entry, _ = _class_location(application, owner, current)
    url = entry.resolve(strict=True).as_uri().replace("file:///", "file:/", 1)
    return url + ("/" if entry.is_dir() and not url.endswith("/") else "")


def configure(application: PreparedApplication, policy: Policy, runtime: Path,
              bindings_file: Path, application_identity: dict[str, Any],
              tooling: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate a private producer plan without compiling or changing application files."""
    if not policy.data["traces"]["enabled"] or policy.data["traces"]["sample_ratio"] < 1.0:
        raise ValueError("the witness profile requires explicit tracing with sample_ratio=1")
    document, binding_identity = _binding_snapshot(bindings_file, application)
    selected = json.loads((runtime / "selection.json").read_text())
    target = document["bindings"]["target"]
    if target["name"] not in selected["methods"].get(target["owner"].replace("/", "."), []):
        raise ValueError("witness target is not selected by the common policy")
    from otelc.prepared_java import file_identity

    values = {"schema_version": "1", "profile": PROFILE,
              "socket": str(runtime / "witness.sock"), "build": digest(application_identity),
              "selection": hashlib.sha256((runtime / "selection.json").read_bytes().rstrip(b"\n")).hexdigest(),
              "policy": digest(policy.data), "tooling": digest([*tooling, binding_identity])}
    for role, binding in document["bindings"].items():
        values.update({f"{role}.{key}": value for key, value in binding.items()})
        values[f"{role}.origin"] = origin(application, binding["owner"], identity=application_identity)
    plan = runtime / "witness.properties"
    plan.write_text(java.properties(values))
    metadata = {"schema_version": 1, "kind": "otelc.java-witness-launch", "profile": PROFILE,
                "socket": values["socket"], "final_report": str(runtime / "witness-health.json"), "build_sha256": values["build"],
                "selection_sha256": values["selection"], "policy_sha256": values["policy"],
                "tooling_sha256": values["tooling"], "bindings": document["bindings"],
                "bindings_sha256": binding_identity["sha256"],
                "runtime_confirmation_required": True, "receiver_confirmation_required": True}
    descriptor = runtime / "witness.json"
    descriptor.write_text(json.dumps(metadata, indent=2) + "\n")
    return [binding_identity, file_identity(plan), file_identity(descriptor)]


def request(path: Path, operation: str, *arguments: str) -> dict[str, Any]:
    """Exchange a bounded correlated request with an owned private producer socket."""
    metadata = path.lstat()
    if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise ValueError("witness endpoint must be an owned private socket")
    request_id = uuid.uuid4().hex
    command = " ".join(("1", request_id, operation, *arguments)) + "\n"
    if len(command) > 2049 or not re.fullmatch(r"[a-z0-9 ]+\n", command):
        raise ValueError("invalid witness request")
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(4)
        channel.connect(str(path))
        channel.sendall(command.encode("ascii"))
        data = bytearray()
        while len(data) <= 262144 and not data.endswith(b"\n"):
            part = channel.recv(16384)
            if not part:
                break
            data.extend(part)
    if len(data) > 262144 or not data.endswith(b"\n"):
        raise ValueError("incomplete or oversized witness response")
    from otelc.prepared_java import unique_object

    result = json.loads(data, object_pairs_hook=unique_object)
    return validate_reply(result, request_id)
