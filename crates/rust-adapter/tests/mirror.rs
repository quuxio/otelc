use quux_otelc_rust_adapter::compiler::mirror;
use std::{
    fs,
    os::unix::fs::{symlink, PermissionsExt},
};

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

#[test]
fn rust_symlink_inputs_are_private_copies_even_when_target_is_external() {
    let project = tempfile::tempdir().unwrap();
    let dependency = tempfile::tempdir().unwrap();
    let original = dependency.path().join("original.rs");
    fs::write(&original, "pub fn value() -> u32 { 42 }\n").unwrap();
    let input = project.path().join("linked.rs");
    symlink(&original, &input).unwrap();
    let elsewhere = tempfile::tempdir().unwrap();
    let generated = elsewhere.path().join("generated");
    mirror(project.path(), &generated).unwrap();
    assert!(!generated.join("linked.rs").is_symlink());
    fs::write(generated.join("linked.rs"), "private instrumented source\n").unwrap();
    assert!(input.is_symlink());
    assert_eq!(
        fs::read_to_string(input).unwrap(),
        "pub fn value() -> u32 { 42 }\n"
    );
    assert_eq!(
        fs::read_to_string(original).unwrap(),
        "pub fn value() -> u32 { 42 }\n"
    );
}

#[test]
fn linked_source_directories_are_private_and_cycles_fail_closed() {
    let project = tempfile::tempdir().unwrap();
    let dependency = tempfile::tempdir().unwrap();
    fs::write(
        dependency.path().join("lib.rs"),
        "original dependency source\n",
    )
    .unwrap();
    symlink(dependency.path(), project.path().join("modules")).unwrap();
    let elsewhere = tempfile::tempdir().unwrap();
    let generated = elsewhere.path().join("generated");
    mirror(project.path(), &generated).unwrap();
    assert!(!generated.join("modules").is_symlink());
    fs::write(generated.join("modules/lib.rs"), "private source\n").unwrap();
    assert_eq!(
        fs::read_to_string(dependency.path().join("lib.rs")).unwrap(),
        "original dependency source\n"
    );
    symlink(project.path(), project.path().join("cycle")).unwrap();
    let error = mirror(project.path(), &elsewhere.path().join("with-cycle")).unwrap_err();
    assert!(error.to_string().contains("cyclic Rust source directory"));
}

#[test]
fn dangling_rust_source_links_are_rejected_without_replacing_original_links() {
    let project = tempfile::tempdir().unwrap();
    symlink("missing.rs", project.path().join("input.rs")).unwrap();
    let elsewhere = tempfile::tempdir().unwrap();
    assert!(mirror(project.path(), &elsewhere.path().join("generated")).is_err());
    assert!(project.path().join("input.rs").is_symlink());
    assert_eq!(
        fs::read_link(project.path().join("input.rs")).unwrap(),
        std::path::Path::new("missing.rs")
    );
}

#[test]
fn readonly_inputs_and_stale_private_links_never_write_back_to_source() {
    let project = tempfile::tempdir().unwrap();
    let original = project.path().join("main.rs");
    fs::write(&original, "original source\n").unwrap();
    fs::set_permissions(&original, fs::Permissions::from_mode(0o400)).unwrap();
    let elsewhere = tempfile::tempdir().unwrap();
    let generated = elsewhere.path().join("generated");
    fs::create_dir(&generated).unwrap();
    symlink(&original, generated.join("main.rs")).unwrap();
    mirror(project.path(), &generated).unwrap();
    assert!(!generated.join("main.rs").is_symlink());
    fs::write(generated.join("main.rs"), "private probes\n").unwrap();
    assert_eq!(fs::read_to_string(&original).unwrap(), "original source\n");
    assert_eq!(
        fs::metadata(&original).unwrap().permissions().mode() & 0o777,
        0o400
    );
}
