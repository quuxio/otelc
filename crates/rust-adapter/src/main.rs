fn main() {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let result = if std::env::var_os("OTELC_RUST_GENERATED").is_some() {
        quux_otelc_rust_adapter::compiler::wrapper(args)
    } else {
        args.first()
            .ok_or_else(|| anyhow::anyhow!("Rust adapter requires a resolved policy"))
            .and_then(|plan| {
                let options: Vec<String> = args[1..]
                    .iter()
                    .map(|value| value.to_string_lossy().into_owned())
                    .collect();
                quux_otelc_rust_adapter::compiler::run(
                    std::path::Path::new(plan),
                    &options,
                    &std::env::current_exe()?,
                )
            })
    };
    match result {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("otelc Rust: {error:#}");
            std::process::exit(1);
        }
    }
}
