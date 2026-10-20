//! Compiler protocol boundaries and original-byte coordinate contracts.
use quux_otelc_config::common::{CommonConfig, Language};
use quux_otelc_rust::policy::Plan;
use quux_otelc_rust_adapter::compiler_frontend::{position, Request};
use quux_otelc_rust_adapter::compiler_inventory::source_definitions;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

fn request() -> Request {
    let resolved = CommonConfig::load(Path::new("../../examples/rust.toml"), true)
        .unwrap()
        .resolve(Language::Rust)
        .unwrap();
    let mut policy: Plan = serde_json::from_value(serde_json::to_value(resolved).unwrap()).unwrap();
    policy.backend = "llvm".into();
    policy.functions.include = vec!["app.*".into()];
    let source = "fn chosen(){let closure=||7;}";
    Request {
        schema_version: 1,
        root: "/original".into(),
        entry: "/original/app.rs".into(),
        declarations: source_definitions(
            source,
            Path::new("/original/app.rs"),
            "app",
            &policy,
            true,
            false,
        )
        .unwrap(),
        policy,
        sources: BTreeMap::from([(PathBuf::from("/original/app.rs"), "0".repeat(64))]),
        protected_source_roots: vec!["/sdk".into()],
        discovery_argv: vec!["app.rs".into()],
    }
}

#[test]
fn original_coordinates_count_utf8_bytes_crlf_and_encoding_markers() {
    let source = "\u{feff}λ\r\nbody";
    for (offset, expected) in [(3, (1, 1)), (5, (1, 3)), (7, (2, 1)), (11, (2, 5))] {
        assert_eq!(position(source, offset).unwrap(), expected);
    }
    assert!(position(source, 4).is_err());
    assert!(position(source, 12).is_err());
    assert_eq!(position("", 0).unwrap(), (1, 1));
}

#[test]
fn protocol_rejects_wrong_backend_version_roots_hashes_and_runtime_limits() {
    let temporary = tempfile::tempdir().unwrap();
    let path = temporary.path().join("request.json");
    let valid = serde_json::to_value(request()).unwrap();
    fs::write(&path, serde_json::to_vec(&valid).unwrap()).unwrap();
    assert_eq!(Request::load(&path).unwrap().declarations.len(), 2);
    for (pointer, value) in [
        ("/schema_version", json!(2)),
        ("/root", json!("/other")),
        ("/entry", json!("/outside.rs")),
        ("/policy/backend", json!("compiler")),
        ("/protected_source_roots", json!([])),
        ("/sources", json!({})),
        ("/policy/runtime/max_active_calls", json!(0)),
    ] {
        let mut invalid = valid.clone();
        *invalid.pointer_mut(pointer).unwrap() = value;
        fs::write(&path, serde_json::to_vec(&invalid).unwrap()).unwrap();
        assert!(Request::load(&path).is_err(), "{pointer}");
    }
    let mut invalid: Value = valid;
    invalid["sources"]["/original/app.rs"] = json!("A".repeat(64));
    fs::write(&path, serde_json::to_vec(&invalid).unwrap()).unwrap();
    assert!(Request::load(&path).is_err());
    fs::write(&path, vec![b'x'; 8 * 1024 * 1024 + 1]).unwrap();
    assert!(Request::load(&path)
        .err()
        .unwrap()
        .to_string()
        .contains("8 MiB"));
}

#[test]
fn original_aliases_require_exact_owner_ranges_and_refuse_ambiguous_matches() {
    let mut request = request();
    let function = request.declarations[0].clone();
    let site = &function.site;
    let body = (
        (site.body_start_line, site.body_start_column),
        (site.body_end_line, site.body_end_column),
    );
    let declaration = (site.declaration_start_line, site.declaration_start_column);
    assert_eq!(
        request
            .original(&function.file, body, declaration, false)
            .unwrap()
            .unwrap()
            .name,
        "app.chosen"
    );
    assert!(request
        .original("/other.rs", body, declaration, false)
        .unwrap()
        .is_none());
    assert!(request
        .original(&function.file, ((99, 1), (99, 2)), declaration, false)
        .unwrap()
        .is_none());
    let closure = request.declarations[1].clone();
    assert!(request
        .original(
            &closure.file,
            ((0, 0), (0, 0)),
            (
                closure.site.declaration_start_line,
                closure.site.declaration_start_column
            ),
            true
        )
        .unwrap()
        .is_some());
    request.declarations.push(function.clone());
    assert!(request
        .original(&function.file, body, declaration, false)
        .is_err());
}
