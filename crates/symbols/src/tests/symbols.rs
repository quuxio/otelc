use super::*;
#[test]
fn adjacent_manifest() {
    assert_eq!(manifest_path(Path::new("app")), Path::new("app.otelc.json"));
}
#[test]
fn malformed_executable() {
    assert!(image_info(Path::new("Cargo.toml")).is_err());
}
#[test]
fn missing_manifest() {
    assert!(Manifest::load(Path::new("missing.otelc.json")).is_err());
}

fn function(linkage: &str, name: &str, selected: bool) -> Function {
    Function {
        address: 1,
        linkage_name: linkage.into(),
        display_name: name.into(),
        selection_name: None,
        selected,
        annotated: false,
        reason: "fixture".into(),
    }
}
#[test]
fn different_cpp_bodies_keep_stable_selection_and_distinct_labels() {
    let original = "Cleanup::~Cleanup()";
    let mut functions = vec![
        function("_ZN7CleanupD1Ev", original, true),
        function("_ZN7CleanupD2Ev", original, true),
        function("_ZN7CleanupD2Ev", original, false),
        function("ordinary", "ordinary", true),
    ];
    distinguish_cpp_linkages(&mut functions).unwrap();
    for body in &functions[..2] {
        assert_eq!(body.selection_name.as_deref(), Some(original));
        assert!(body.display_name.starts_with(original));
        assert!(body.display_name.contains(&body.linkage_name));
        let restored: Function =
            serde_json::from_str(&serde_json::to_string(body).unwrap()).unwrap();
        assert_eq!(restored.selection_name, body.selection_name);
    }
    assert_ne!(functions[0].display_name, functions[1].display_name);
    assert_eq!(functions[2].display_name, original);
    assert!(functions[2].selection_name.is_none());
    assert!(!serde_json::to_string(&functions[3])
        .unwrap()
        .contains("selection_name"));
}
#[test]
fn identical_linkages_c_names_and_misidentified_cpp_names_remain_rejected() {
    for (left, right, name) in [
        ("_ZN7CleanupD1Ev", "_ZN7CleanupD1Ev", "Cleanup::~Cleanup()"),
        ("local", "local", "local"),
        ("_ZN7CleanupD1Ev", "other", "Cleanup::~Cleanup()"),
        ("_ZN7CleanupD1Ev", "_ZN7CleanupD2Ev", "wrong name"),
    ] {
        let mut functions = vec![function(left, name, true), function(right, name, true)];
        assert!(distinguish_cpp_linkages(&mut functions).is_err());
    }
    let mut functions = vec![
        function("_ZN7CleanupD1Ev", "Cleanup::~Cleanup()", true),
        function("_ZN7CleanupD2Ev", "Cleanup::~Cleanup()", true),
        function(
            "literal",
            "Cleanup::~Cleanup() [linkage=_ZN7CleanupD1Ev]",
            true,
        ),
    ];
    assert!(distinguish_cpp_linkages(&mut functions).is_err());
}
