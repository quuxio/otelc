use super::*;
#[test]
fn unsupported_combinations() {
    for option in [
        "-flto=thin",
        "-shared",
        "-fsanitize=address",
        "--target=aarch64-linux",
        "-finstrument-functions",
    ] {
        assert!(unsupported(&[option.into()]).is_err());
    }
    assert!(unsupported(&["-O2".into(), "-g".into(), "-fno-exceptions".into()]).is_ok());
}
#[test]
fn source_detection() {
    assert!(source("file with spaces.cpp"));
    assert!(source("main.c"));
    assert!(!source("app.o"));
}
