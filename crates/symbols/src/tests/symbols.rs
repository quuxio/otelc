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
