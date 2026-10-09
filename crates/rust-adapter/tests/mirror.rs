use quux_otelc_rust_adapter::compiler::mirror;
use std::{fs, os::unix::fs::symlink};

#[test]
fn nested_build_workspace_is_rejected_before_creating_generated_source() {
    let project = tempfile::tempdir().unwrap();
    fs::write(project.path().join("main.rs"), "fn main() {}\n").unwrap();
    let generated = project.path().join("generated");
    let error = mirror(project.path(), &generated).unwrap_err();
    assert!(error.to_string().contains("outside the source tree"));
    assert!(!generated.exists());
    assert_eq!(
        fs::read_to_string(project.path().join("main.rs")).unwrap(),
        "fn main() {}\n"
    );
}

#[test]
fn equal_or_symlink_aliased_workspace_is_also_rejected() {
    let project = tempfile::tempdir().unwrap();
    assert!(mirror(project.path(), project.path())
        .unwrap_err()
        .to_string()
        .contains("outside the source tree"));
    let elsewhere = tempfile::tempdir().unwrap();
    let alias = elsewhere.path().join("source-alias");
    symlink(project.path(), &alias).unwrap();
    assert!(mirror(project.path(), &alias.join("generated"))
        .unwrap_err()
        .to_string()
        .contains("outside the source tree"));
    assert!(!project.path().join("generated").exists());
}

#[test]
fn external_workspace_copies_rust_and_preserves_other_inputs() {
    let project = tempfile::tempdir().unwrap();
    fs::create_dir(project.path().join("nested")).unwrap();
    fs::write(
        project.path().join("nested/lib.rs"),
        "pub fn value() -> u32 { 42 }\n",
    )
    .unwrap();
    fs::write(project.path().join("Cargo.toml"), "original manifest\n").unwrap();
    let elsewhere = tempfile::tempdir().unwrap();
    let generated = elsewhere.path().join("generated");
    mirror(project.path(), &generated).unwrap();
    fs::write(
        generated.join("nested/lib.rs"),
        "private generated source\n",
    )
    .unwrap();
    assert_eq!(
        fs::read_to_string(project.path().join("nested/lib.rs")).unwrap(),
        "pub fn value() -> u32 { 42 }\n"
    );
    assert!(generated.join("Cargo.toml").is_symlink());
    assert_eq!(
        fs::read_to_string(generated.join("Cargo.toml")).unwrap(),
        "original manifest\n"
    );
}
