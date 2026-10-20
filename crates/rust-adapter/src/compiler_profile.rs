//! Qualified compiler/SDK/pass identities; never select an arbitrary nightly.
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::Command,
};

pub(crate) const LLVM_BASELINE: [&str; 5] = [
    "--edition=2024",
    "-O",
    "-g",
    "-Zmir-enable-passes=-Inline,-ForceInline",
    "-Ccodegen-units=1",
];
const COMMIT: &str = "d080e7dff1b0fc54541545252818f8cccf995d05";

pub(crate) fn hash(path: &Path) -> Result<String> {
    let mut input = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut bytes = [0; 65536];
    loop {
        let count = input.read(&mut bytes)?;
        if count == 0 {
            break;
        }
        digest.update(&bytes[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Profile {
    schema_version: u32,
    pub rustc: PathBuf,
    pub sdk: PathBuf,
    pub plugin: PathBuf,
    pub frontend: PathBuf,
    pub sdk_sha256: String,
    pub plugin_sha256: String,
    pub frontend_sha256: String,
    llvm_version: String,
    pub protected_source_roots: Vec<PathBuf>,
    #[serde(skip)]
    pub fingerprint: String,
    #[serde(skip)]
    pub bytes: Vec<u8>,
}

impl Profile {
    pub fn load(path: &Path) -> Result<Self> {
        if !fs::symlink_metadata(path)
            .context("Rust compiler profile is missing; run make rust-llvm-build")?
            .is_file()
        {
            bail!("Rust compiler profile must be a regular file");
        }
        let mut bytes = Vec::new();
        fs::File::open(path)?.take(65537).read_to_end(&mut bytes)?;
        if bytes.len() > 65536 {
            bail!("Rust compiler profile exceeds 64 KiB");
        }
        let mut profile: Self =
            serde_json::from_slice(&bytes).context("read Rust compiler profile")?;
        if profile.schema_version != 2
            || profile.llvm_version != "23.1.3"
            || !profile.rustc.is_absolute()
            || !profile.sdk.is_absolute()
            || !profile.plugin.is_absolute()
            || !profile.frontend.is_absolute()
            || profile.protected_source_roots.is_empty()
            || profile.protected_source_roots.len() > 16
            || profile.protected_source_roots.iter().any(|path| {
                !path.is_absolute()
                    || path
                        .components()
                        .any(|part| matches!(part, std::path::Component::ParentDir))
            })
        {
            bail!("invalid qualified Rust compiler profile; run make rust-llvm-build");
        }
        for (path, expected) in [
            (&profile.sdk, &profile.sdk_sha256),
            (&profile.plugin, &profile.plugin_sha256),
            (&profile.frontend, &profile.frontend_sha256),
        ] {
            if !fs::symlink_metadata(path)?.is_file()
                || expected.len() != 64
                || hash(path)? != *expected
            {
                bail!("Rust compiler SDK/pass/frontend fingerprint mismatch; rebuild the compiler profile");
            }
        }
        profile.fingerprint = format!("{:x}", Sha256::digest(&bytes));
        profile.bytes = bytes;
        Ok(profile)
    }

    pub fn doctor(&self, rustc: &str) -> Result<()> {
        let version = Command::new(rustc)
            .arg("-vV")
            .output()
            .context("locate qualified nightly rustc")?;
        Self::validate_version(
            version.status.success(),
            &String::from_utf8_lossy(&version.stdout),
        )
    }

    fn validate_version(success: bool, version: &str) -> Result<()> {
        if !success
            || !version.starts_with("rustc 1.101.0-nightly ")
            || !version
                .lines()
                .any(|line| line == format!("commit-hash: {COMMIT}"))
            || !version
                .lines()
                .any(|line| line == "host: aarch64-apple-darwin")
            || !version.lines().any(|line| line == "LLVM version: 23.1.1")
        {
            bail!("Rust LLVM backend requires the qualified 1.101.0-nightly d080e7dff macOS ARM64 compiler (LLVM 23.1.1)");
        }
        Ok(())
    }
}

pub(crate) struct Toolchain {
    pub rustc: String,
    pub sdk: PathBuf,
    pub llvm: Option<Profile>,
}

impl Toolchain {
    pub fn load(backend: &str, adapter: &Path) -> Result<Self> {
        let directory = adapter.parent().context("Rust adapter directory")?;
        let llvm = if backend == "llvm" {
            Some(Profile::load(&directory.join("otelc-rust-llvm.json"))?)
        } else {
            None
        };
        let rustc = std::env::var("OTELC_RUSTC").unwrap_or_else(|_| {
            llvm.as_ref().map_or_else(
                || "rustc".into(),
                |profile| profile.rustc.to_string_lossy().into_owned(),
            )
        });
        let sdk = llvm.as_ref().map_or_else(
            || directory.join("libquux_otelc_rust.rlib"),
            |profile| profile.sdk.clone(),
        );
        if !sdk.is_file() {
            bail!("Rust SDK rlib is missing; run make build");
        }
        Ok(Self { rustc, sdk, llvm })
    }

    pub fn doctor(&self) -> Result<()> {
        if std::env::var_os("RUSTC_WRAPPER").is_some()
            || std::env::var_os("RUSTC_WORKSPACE_WRAPPER").is_some()
        {
            bail!("existing Rust compiler wrappers require separate qualification");
        }
        if let Some(profile) = &self.llvm {
            profile.doctor(&self.rustc)
        } else {
            crate::compiler::doctor(&self.rustc)
        }
    }

    pub fn baseline_flags(&self) -> Vec<String> {
        if self.llvm.is_some() {
            LLVM_BASELINE.map(str::to_owned).to_vec()
        } else {
            crate::compiler::BASELINE_FLAGS.map(str::to_owned).to_vec()
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct HookEvidence {
    pub profile_sha256: String,
    pub plugin_sha256: String,
    pub inventory_sha256: String,
    pub resolved_sha256: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compiler_profile_requires_the_complete_qualified_identity() {
        let version = format!("rustc 1.101.0-nightly (d080e7dff 2026-09-27)\ncommit-hash: {COMMIT}\nhost: aarch64-apple-darwin\nLLVM version: 23.1.1\n");
        assert!(Profile::validate_version(true, &version).is_ok());
        for changed in [
            version.replace(COMMIT, "other"),
            version.replace("23.1.1", "23.1.2"),
            version.replace("aarch64-apple-darwin", "x86_64-apple-darwin"),
            version.replace("1.101.0-nightly", "1.101.0"),
        ] {
            assert!(Profile::validate_version(true, &changed).is_err());
        }
        assert!(Profile::validate_version(false, &version).is_err());
    }

    #[test]
    fn profile_binds_the_frontend_and_refuses_older_or_changed_inputs() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        for name in ["sdk.rlib", "pass.dylib", "frontend"] {
            fs::write(root.join(name), name).unwrap();
        }
        let path = root.join("profile.json");
        let value = serde_json::json!({"schema_version":2,"rustc":"/compiler/bin/rustc",
            "sdk":root.join("sdk.rlib"),"sdk_sha256":hash(&root.join("sdk.rlib")).unwrap(),
            "plugin":root.join("pass.dylib"),"plugin_sha256":hash(&root.join("pass.dylib")).unwrap(),
            "frontend":root.join("frontend"),"frontend_sha256":hash(&root.join("frontend")).unwrap(),
            "llvm_version":"23.1.3","protected_source_roots":["/sdk"]});
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        assert!(Profile::load(&path).is_ok());
        fs::write(root.join("frontend"), b"changed compiler frontend").unwrap();
        assert!(Profile::load(&path)
            .err()
            .unwrap()
            .to_string()
            .contains("fingerprint"));
        fs::write(root.join("frontend"), b"frontend").unwrap();
        for (key, replacement) in [
            ("schema_version", serde_json::json!(1)),
            ("frontend", serde_json::json!("relative/frontend")),
        ] {
            let mut changed = value.clone();
            changed[key] = replacement;
            fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
            assert!(Profile::load(&path).is_err());
        }
    }
}
