//! Original declaration metadata is independent of compiler eligibility and insertion.
use quux_otelc_config::common::{CommonConfig, Language};
use quux_otelc_rust::policy::Plan;
use quux_otelc_rust_adapter::compiler_inventory::{source_definitions, Definition};
use std::path::Path;

fn policy() -> Plan {
    serde_json::from_value(
        serde_json::to_value(
            CommonConfig::load(Path::new("../../examples/rust.toml"), true)
                .unwrap()
                .resolve(Language::Rust)
                .unwrap(),
        )
        .unwrap(),
    )
    .unwrap()
}

fn inventory(
    source: &str,
    plan: &Plan,
    selected_source: bool,
    startup: bool,
) -> anyhow::Result<Vec<Definition>> {
    source_definitions(
        source,
        Path::new("/original/file.rs"),
        "file",
        plan,
        selected_source,
        startup,
    )
}

#[test]
fn positions_filters_and_optional_annotations_use_original_input() {
    let mut plan = policy();
    plan.functions.include = vec!["file.configured".into()];
    plan.functions.exclude = vec!["file.excluded".into()];
    let source = "// Unicode: λ and CRLF\r\n// otelc.instrument\r\nfn tagged(__quux_otelc_guard: i32) -> i32 { __quux_otelc_guard }\r\nfn configured() {}\r\n// otelc.instrument\r\nfn excluded() {}\r\n// otelc.instrument\r\n\r\nfn separate() {}\r\nfn main() {}\r\n";
    let functions = inventory(source, &plan, true, true).unwrap();
    assert_eq!(
        functions
            .iter()
            .map(|function| function.selected)
            .collect::<Vec<_>>(),
        [true, true, false, false, false]
    );
    assert_eq!(functions[0].site.header_start, 3);
    assert!(functions.last().unwrap().site.startup);
    assert!(inventory(source, &plan, false, false)
        .unwrap()
        .iter()
        .all(|function| !function.selected));
    plan.annotations.read_existing = false;
    plan.annotations.inject_generated = false;
    let functions = inventory(source, &plan, true, false).unwrap();
    assert!(!functions[0].selected);
    assert!(functions[1].selected);
}

#[test]
fn nested_closures_keep_distinct_original_declarations() {
    let mut plan = policy();
    plan.functions.include = vec!["*.<closure>@*".into()];
    let source = "fn main() { let expression=|value:i32| value+1; let block=move |value:i32| {value+2}; let nested=|| || 7; }";
    let functions = inventory(source, &plan, true, false).unwrap();
    let closures: Vec<_> = functions
        .iter()
        .filter(|function| function.name.contains("<closure>"))
        .collect();
    assert_eq!(closures.len(), 4);
    assert!(closures
        .iter()
        .all(|function| function.selected && function.unsupported.is_empty()));
    let names: std::collections::HashSet<_> =
        closures.iter().map(|function| &function.name).collect();
    assert_eq!(names.len(), 4);
    assert!(closures
        .iter()
        .all(|function| function.site.declaration_start_line == 1));
}

#[test]
fn closure_annotations_exclusions_and_qualification_metadata_are_retained() {
    let mut plan = policy();
    plan.functions.include = vec!["file.main.<closure>@4:*".into()];
    let source = "fn main(){\n// otelc.instrument\nlet annotated=||7;\nlet configured=||8;\n// otelc.exclude\nlet excluded=||9;\n}";
    let functions = inventory(source, &plan, true, false).unwrap();
    assert_eq!(
        functions
            .iter()
            .map(|function| function.selected)
            .collect::<Vec<_>>(),
        [false, true, true, false]
    );
    plan.functions.exclude.push("*.<closure>@3:*".into());
    assert!(!inventory(source, &plan, true, false).unwrap()[1].selected);
    plan.functions.include = vec!["not_selected".into()];
    plan.functions.exclude.clear();
    assert!(
        inventory(
            "fn main(){\n/// otelc.instrument\nlet callback=||7;\n}",
            &plan,
            true,
            false
        )
        .unwrap()[1]
            .selected
    );
    plan.functions.include = vec!["*".into()];
    for source in [
        "fn main(){ let callback=async || 7; }",
        "fn main(){ let callback=const || 7; }",
    ] {
        assert!(!inventory(source, &plan, true, false).unwrap()[1]
            .unsupported
            .is_empty());
        assert!(!inventory(source, &plan, false, false).unwrap()[1].selected);
    }
    assert!(inventory(
        "fn main(){\n// otelc.unknown\nlet callback=||7;}",
        &plan,
        true,
        false
    )
    .is_err());
}

#[test]
fn generic_trait_nested_and_raw_identifier_scopes_keep_their_names() {
    let mut plan = policy();
    plan.functions.include = vec!["*".into()];
    plan.functions.exclude.clear();
    let source = "mod book { pub fn price<T>(v:T)->T { fn nested() {} nested(); v } } struct Order; impl Order { fn r#type(&self) {} } trait Calculate { fn default_value(&self) -> i32 { 3 } fn abstract_value(&self); } impl Calculate for Order { fn abstract_value(&self) {} } impl<T> Calculate for [T; 3] { fn abstract_value(&self) {} }";
    let functions = inventory(source, &plan, true, false).unwrap();
    assert_eq!(
        functions
            .iter()
            .map(|function| function.name.as_str())
            .collect::<Vec<_>>(),
        [
            "file.book.price",
            "file.book.price.nested",
            "file.Order.r#type",
            "file.Calculate.default_value",
            "file.<Order as Calculate>.abstract_value",
            "file.<[T ; 3] as Calculate>.abstract_value"
        ]
    );
    plan.functions.include = vec!["file.λ?".into()];
    assert!(inventory("fn λx() {}", &plan, true, false).unwrap()[0].selected);
}

#[test]
fn actual_compiler_owners_determine_entry_and_construct_eligibility() {
    let mut plan = policy();
    plan.functions.include = vec!["*".into()];
    plan.functions.exclude.clear();
    for source in [
        "const fn fixed() -> i32 { 2 }",
        "async fn patterned((x, y): (i32, i32)) -> i32 { x + y }",
        "async fn referenced(ref value: String) -> usize { value.len() }",
        "#[unsafe(naked)] unsafe extern \"C\" fn bare() {}",
    ] {
        assert!(!inventory(source, &plan, true, false).unwrap()[0]
            .unsupported
            .is_empty());
        assert!(!inventory(source, &plan, false, false).unwrap()[0].selected);
    }
    let entries = inventory(
        "#[cfg(any())] async fn main(){} fn main(){}",
        &plan,
        false,
        true,
    )
    .unwrap();
    assert_eq!(entries.len(), 2);
    assert!(entries.iter().all(|function| function.site.startup));
    assert!(inventory("include!(\"part.rs\");", &plan, true, false)
        .unwrap()
        .is_empty());
    for source in ["// otelc.unknown\nfn wrong() {}", "fn broken("] {
        assert!(inventory(source, &plan, true, false).is_err());
    }
    let functions = inventory(
        "/// otelc.instrument\nfn doc() {}\n// otelc.exclude\nfn excluded() {}",
        &plan,
        true,
        false,
    )
    .unwrap();
    assert!(functions[0].selected);
    assert!(!functions[1].selected);
}

#[test]
fn specialised_receivers_and_nested_trait_methods_have_distinct_identities() {
    let mut plan = policy();
    plan.functions.include = vec!["*".into()];
    plan.functions.exclude.clear();
    let source = "struct Slot<T>(T); impl Slot<u32> { fn value(&self) {} } impl Slot<u64> { fn value(&self) {} } impl first::Order { fn value(&self) {} } impl second::Order { fn value(&self) {} } trait Defaulted { fn first(&self) { fn helper() {} helper(); } fn second(&self) { fn helper() {} helper(); } } fn marker_text()->&'static str { \"quux.otelc.generated\" }";
    let functions = inventory(source, &plan, true, false).unwrap();
    let names: std::collections::HashSet<_> = functions
        .iter()
        .map(|function| function.name.as_str())
        .collect();
    assert_eq!(names.len(), functions.len());
    assert!(names.contains("file.Defaulted.first.helper"));
    assert!(names.contains("file.Defaulted.second.helper"));
    assert!(names.contains("file.Slot < u32 >.value"));
    assert!(names.contains("file.Slot < u64 >.value"));
}
