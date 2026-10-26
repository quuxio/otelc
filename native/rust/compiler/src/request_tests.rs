//! Exercise original input bindings without invoking rustc or the SDK.
use super::{Request, digest};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);

struct Fixture {
    directory: PathBuf,
    request: PathBuf,
    value: Value,
}

impl Fixture {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!(
            "otelc-rust-request-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&directory).expect("create distinct test directory");
        let directory = directory.canonicalize().expect("canonical test directory");
        let root = directory.join("app");
        fs::create_dir(&root).expect("create application directory");
        let entry = root.join("app.rs");
        let source = b"fn main() {}\n";
        fs::write(&entry, source).expect("write original source");
        Self {
            request: directory.join("request.json"),
            value: json!({"schema_version":1,"mode":"inspect","root":root,
                "entry":entry,"output":directory.join("inventory.json"),
                "sources":{entry.to_str().expect("test entry UTF-8"):digest(source)},
                "selected":[]}),
            directory,
        }
    }

    fn load(&self, value: &Value) -> anyhow::Result<Request> {
        fs::write(&self.request, serde_json::to_vec(value)?)?;
        Request::load(&self.request)
    }

    fn rejects(&self, value: &Value) {
        assert!(
            self.load(value).is_err(),
            "accepted altered request: {value}"
        );
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).expect("remove test-owned inputs");
    }
}

#[test]
fn accepts_both_modes_without_changing_original_sources() {
    let fixture = Fixture::new();
    let original = fs::read(fixture.value["entry"].as_str().unwrap()).unwrap();
    let request = fixture.load(&fixture.value).expect("valid inspect request");
    assert_eq!(request.mode, "inspect");
    assert!(request.selected.is_empty());
    let mut weave = fixture.value.clone();
    weave["mode"] = json!("weave");
    weave["selected"] = json!(["app.rs::main"]);
    assert!(
        fixture
            .load(&weave)
            .unwrap()
            .selected
            .contains("app.rs::main")
    );
    assert_eq!(fs::read(request.entry).unwrap(), original);
    assert!(!request.output.exists());
}

#[test]
fn lifetime_discovery_is_explicit_without_function_selection() {
    let fixture = Fixture::new();
    let mut value = fixture.value.clone();
    value["mode"] = json!("lifetime-inspect");
    assert_eq!(fixture.load(&value).unwrap().mode, "lifetime-inspect");
    value["selected"] = json!(["app.rs::main"]);
    fixture.rejects(&value);
}

#[test]
fn module_discovery_never_enables_weaving_or_selected_function_calls() {
    let fixture = Fixture::new();
    let mut value = fixture.value.clone();
    value["mode"] = json!("module-inspect");
    assert!(!fixture.load(&value).unwrap().weaving());
    value["selected"] = json!(["app.rs::main"]);
    fixture.rejects(&value);
    value["selected"] = json!([]);
    value["schema_version"] = json!(2);
    fixture.rejects(&value);
    assert!(!PathBuf::from(fixture.value["output"].as_str().unwrap()).exists());
}

#[test]
fn lifetime_weaving_requires_a_separate_closed_nonempty_local_binding_profile() {
    let fixture = Fixture::new();
    let mut value = fixture.value.clone();
    value["schema_version"] = json!(2);
    value["mode"] = json!("lifetime-weave");
    value["lifetimes"] = json!({"app.rs::workload":{"1":"app::Item"}});
    value["lifetime_bodies"] = json!({"app.rs::workload":"a".repeat(64)});
    let request = fixture
        .load(&value)
        .expect("valid private lifetime binding");
    assert!(request.weaving());
    assert_eq!(
        request.lifetimes.unwrap()["app.rs::workload"][&1],
        "app::Item"
    );
    for (field, replacement) in [
        ("schema_version", json!(1)),
        ("schema_version", json!(3)),
        ("mode", json!("weave")),
        ("mode", json!("lifetime-inspect")),
        ("lifetimes", json!(null)),
        ("lifetimes", json!({})),
        ("lifetimes", json!({"app.rs::workload":{}})),
        ("lifetimes", json!({"app.rs::workload":{"0":"app::Item"}})),
        (
            "lifetimes",
            json!({"app.rs::workload":{"65536":"app::Item"}}),
        ),
        ("lifetimes", json!({"app.rs::workload":{"1":""}})),
        ("lifetimes", json!({"app.rs::workload":{"1":"bad\0type"}})),
        ("lifetimes", json!({"bad\0body":{"1":"app::Item"}})),
        ("lifetime_bodies", json!(null)),
        ("lifetime_bodies", json!({})),
        ("lifetime_bodies", json!({"another-body":"a".repeat(64)})),
        ("lifetime_bodies", json!({"app.rs::workload":"BAD"})),
    ] {
        let mut changed = value.clone();
        changed[field] = replacement;
        fixture.rejects(&changed);
    }
    let mut legacy = fixture.value.clone();
    legacy["lifetimes"] = value["lifetimes"].clone();
    fixture.rejects(&legacy);
    legacy = fixture.value.clone();
    legacy["lifetime_bodies"] = value["lifetime_bodies"].clone();
    fixture.rejects(&legacy);
}

#[test]
fn rejects_unknown_missing_and_wrongly_typed_fields() {
    let fixture = Fixture::new();
    for field in [
        "schema_version",
        "mode",
        "root",
        "entry",
        "output",
        "sources",
        "selected",
    ] {
        let mut missing = fixture.value.clone();
        missing.as_object_mut().unwrap().remove(field);
        fixture.rejects(&missing);
        let mut wrong = fixture.value.clone();
        wrong[field] = json!(true);
        fixture.rejects(&wrong);
    }
    for (field, replacement) in [
        ("schema_version", json!(2)),
        ("mode", json!("foreign")),
        ("selected", json!(["app.rs::main"])),
        ("sources", json!({})),
        ("extra", json!(1)),
    ] {
        let mut value = fixture.value.clone();
        value[field] = replacement;
        fixture.rejects(&value);
    }
}

#[test]
fn rejects_changed_missing_foreign_or_badly_hashed_sources() {
    let fixture = Fixture::new();
    let entry = fixture.value["entry"].as_str().unwrap();
    for hash in ["", "00", &"A".repeat(64), &"g".repeat(64), &"0".repeat(64)] {
        let mut value = fixture.value.clone();
        value["sources"][entry] = json!(hash);
        fixture.rejects(&value);
    }
    let foreign = fixture.directory.join("foreign.rs");
    fs::write(&foreign, b"fn foreign() {}\n").unwrap();
    let mut value = fixture.value.clone();
    value["sources"][foreign.to_str().unwrap()] = json!(digest(&fs::read(&foreign).unwrap()));
    fixture.rejects(&value);
    fs::write(entry, b"fn main() { panic!(); }\n").unwrap();
    fixture.rejects(&fixture.value);
    fs::remove_file(entry).unwrap();
    fixture.rejects(&fixture.value);
}

#[test]
fn rejects_invalid_root_entry_and_output_boundaries() {
    let fixture = Fixture::new();
    for (field, replacement) in [
        ("root", json!(fixture.directory.join("absent"))),
        ("root", fixture.value["entry"].clone()),
        ("root", json!("relative")),
        ("entry", json!(fixture.directory.join("absent.rs"))),
        ("output", json!("relative.json")),
        ("output", fixture.value["entry"].clone()),
        (
            "output",
            json!(PathBuf::from(fixture.value["root"].as_str().unwrap()).join("out.json")),
        ),
        ("output", json!(fixture.directory.join("absent/out.json"))),
        ("output", json!(fixture.directory.join("app/../out.json"))),
        ("output", json!("/")),
    ] {
        let mut value = fixture.value.clone();
        value[field] = replacement;
        fixture.rejects(&value);
    }
    let existing = fixture.directory.join("existing.json");
    fs::write(&existing, b"original output").unwrap();
    let mut value = fixture.value.clone();
    value["output"] = json!(existing);
    fixture.rejects(&value);
    assert_eq!(fs::read(existing).unwrap(), b"original output");
}

#[cfg(unix)]
#[test]
fn rejects_symlink_source_root_and_output_parent_aliases() {
    use std::os::unix::fs::symlink;
    let fixture = Fixture::new();
    let root_alias = fixture.directory.join("alias");
    symlink(fixture.value["root"].as_str().unwrap(), &root_alias).unwrap();
    let mut value = fixture.value.clone();
    value["root"] = json!(root_alias);
    fixture.rejects(&value);
    let alias = fixture.directory.join("parent-alias");
    symlink(&fixture.directory, &alias).unwrap();
    value = fixture.value.clone();
    value["output"] = json!(alias.join("out.json"));
    fixture.rejects(&value);
    let source_alias = PathBuf::from(fixture.value["root"].as_str().unwrap()).join("alias.rs");
    symlink(fixture.value["entry"].as_str().unwrap(), &source_alias).unwrap();
    value = fixture.value.clone();
    value["sources"][source_alias.to_str().unwrap()] = json!(digest(b"fn main() {}\n"));
    fixture.rejects(&value);
}

#[test]
fn rejects_oversized_request_and_selection_before_compilation() {
    let fixture = Fixture::new();
    fs::write(&fixture.request, vec![b' '; 8 * 1024 * 1024 + 1]).unwrap();
    let error = Request::load(&fixture.request)
        .err()
        .expect("oversized request refused");
    assert!(error.to_string().contains("exceeds 8 MiB"));
    let mut value = fixture.value.clone();
    value["mode"] = json!("weave");
    value["selected"] = json!(
        (0..4097)
            .map(|i| format!("app.rs::f{i}"))
            .collect::<Vec<_>>()
    );
    fixture.rejects(&value);
}

#[test]
fn digest_matches_known_sha256() {
    assert_eq!(
        digest(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}
