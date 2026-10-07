fn main() {
    println!("cargo:rerun-if-changed=src/shim.c");
    println!("cargo:rerun-if-env-changed=CARGO_LLVM_COV");
    let mut build = cc::Build::new();
    build.file("src/shim.c");
    if std::env::var_os("CARGO_LLVM_COV").is_some() {
        build
            .flag("-fprofile-instr-generate")
            .flag("-fcoverage-mapping");
    }
    build.compile("otelc_shim");
}
