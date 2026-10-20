use quux_otelc_config::common::{CommonConfig, Language};
use quux_otelc_rust::policy::Plan;
use quux_otelc_rust_adapter::compiler_inventory::source_definitions;
use serde_json::Value;
use std::path::Path;

fn policy() -> Plan {
    let resolved = CommonConfig::load(Path::new("../../examples/rust.toml"), true)
        .unwrap()
        .resolve(Language::Rust)
        .unwrap();
    let mut plan: Plan = serde_json::from_value(serde_json::to_value(resolved).unwrap()).unwrap();
    plan.functions.include = vec!["file.selected".into()];
    plan.functions.exclude = vec!["file.excluded".into(), "file.main".into()];
    plan
}

fn definitions(source: &str, selected_source: bool, startup: bool) -> Vec<Value> {
    source_definitions(
        source,
        Path::new("/original/file.rs"),
        "file",
        &policy(),
        selected_source,
        startup,
    )
    .unwrap()
    .into_iter()
    .map(|value| serde_json::to_value(value).unwrap())
    .collect()
}

#[test]
fn inventory_uses_original_positions_shared_selection_and_existing_annotations() {
    let source = "// λ\r\n// otelc.instrument\r\n#[inline]\r\npub fn tagged(\r\n) { let λ = 1; let _ = λ; }\r\nfn selected() {} fn excluded() {}\r\nfn main() {}\r\n";
    let rows = definitions(source, true, true);
    assert_eq!(rows.len(), 4);
    assert_eq!(rows[0]["name"], "file.tagged");
    assert_eq!(rows[0]["file"], "/original/file.rs");
    assert_eq!(rows[0]["header_start"], 3);
    assert_eq!(rows[0]["header_end"], 5);
    assert_eq!(rows[0]["body_start_line"], 5);
    assert_eq!(rows[0]["body_start_column"], 3);
    assert_eq!(rows[0]["selected"], true);
    assert_eq!(rows[1]["suffix"], "selected");
    assert_eq!(rows[1]["selected"], true);
    assert_eq!(rows[2]["selected"], false);
    assert_eq!(rows[3]["selected"], false);
    assert_eq!(rows[3]["startup"], true);
    assert!(definitions(source, false, true)
        .iter()
        .all(|row| row["selected"] == false));
}

#[test]
fn same_line_nested_functions_and_closures_retain_distinct_original_ranges() {
    let source = "fn selected() { fn selected() {} let f = || 3; let g = || { 4 }; }";
    let rows = definitions(source, true, false);
    assert_eq!(rows.len(), 4);
    assert_eq!(rows[0]["name"], "file.selected");
    assert_eq!(rows[1]["name"], "file.selected.selected");
    assert_eq!(rows[2]["leaf"], "<closure>");
    assert_eq!(rows[3]["leaf"], "<closure>");
    assert_ne!(rows[2]["body_start_column"], rows[3]["body_start_column"]);
    assert!(
        rows[1]["body_start_column"].as_u64().unwrap()
            > rows[0]["body_start_column"].as_u64().unwrap()
    );
    assert!(
        rows[1]["body_end_column"].as_u64().unwrap() < rows[0]["body_end_column"].as_u64().unwrap()
    );
}

#[test]
fn async_compiler_constructors_are_explicitly_unqualified() {
    let rows = definitions("async fn selected() {}", true, false);
    assert_eq!(rows[0]["selected"], true);
    assert!(rows[0]["unsupported"]
        .as_str()
        .unwrap()
        .contains("future-completion"));
    assert!(!rows[0].as_object().unwrap().contains_key("asynchronous"));
    assert!(source_definitions(
        "fn main() {}",
        Path::new("relative.rs"),
        "file",
        &policy(),
        true,
        true
    )
    .is_err());
}
