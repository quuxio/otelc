//! Identity contract for the original-source stable compiler and its probes.
use crate::compiler_profile::hash;
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::{
    fs,
    io::Read,
    path::{Component, Path, PathBuf},
    process::Command,
};

pub(crate) const COMMIT: &str = "48a229ceaefd4985c50990b14116b6d856af0985";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Profile {
    schema_version: u32,
    backend: String,
    commit_hash: String,
    pub rustc: PathBuf,
    rustc_sha256: String,
    pub sdk: PathBuf,
    sdk_sha256: String,
    pub driver: PathBuf,
    pub driver_sha256: String,
    pub protected_source_roots: Vec<PathBuf>,
    #[serde(skip)]
    pub fingerprint: String,
    #[serde(skip)]
    pub bytes: Vec<u8>,
}

fn absolute(path: &Path) -> bool {
    path.is_absolute()
        && !path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
}

impl Profile {
    pub fn load(path: &Path) -> Result<Self> {
        if !fs::symlink_metadata(path)
            .context("Rust compiler profile is missing; run make rust-mir-build")?
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
            serde_json::from_slice(&bytes).context("read stable Rust compiler profile")?;
        if profile.schema_version != 1
            || profile.backend != "compiler"
            || profile.commit_hash != COMMIT
            || profile.protected_source_roots.is_empty()
            || profile.protected_source_roots.len() > 16
            || profile
                .protected_source_roots
                .iter()
                .any(|root| !absolute(root))
        {
            bail!("invalid stable Rust compiler profile; run make rust-mir-build");
        }
        for (path, expected) in [
            (&profile.rustc, &profile.rustc_sha256),
            (&profile.sdk, &profile.sdk_sha256),
            (&profile.driver, &profile.driver_sha256),
        ] {
            if !absolute(path)
                || !fs::symlink_metadata(path)?.is_file()
                || expected.len() != 64
                || !expected
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                || hash(path)? != *expected
            {
                bail!(
                    "stable Rust compiler/SDK/driver fingerprint mismatch; rebuild the compiler profile"
                );
            }
        }
        // Hash the bytes accepted by the parser, rather than reopening a
        // descriptor that could have been replaced after identity validation.
        use sha2::{Digest, Sha256};
        profile.fingerprint = Sha256::digest(&bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        profile.bytes = bytes;
        Ok(profile)
    }

    pub fn doctor(&self, compiler: &Path) -> Result<()> {
        if compiler.canonicalize()? != self.rustc.canonicalize()?
            || hash(compiler)? != self.rustc_sha256
        {
            bail!("selected Rust compiler differs from its qualified profile");
        }
        let version = Command::new(compiler)
            .arg("-vV")
            .output()
            .context("locate qualified stable rustc")?;
        Self::validate_version(
            version.status.success(),
            &String::from_utf8_lossy(&version.stdout),
        )
    }

    fn validate_version(success: bool, version: &str) -> Result<()> {
        if !success
            || !version.starts_with("rustc 1.98.1 ")
            || !version
                .lines()
                .any(|line| line == format!("commit-hash: {COMMIT}"))
            || !version
                .lines()
                .any(|line| line == "host: aarch64-apple-darwin")
            || !version.lines().any(|line| line == "LLVM version: 22.1.8")
        {
            bail!(
                "Rust compiler backend requires qualified Rust 1.98.1 48a229cea on macOS ARM64 with LLVM 22.1.8"
            );
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::os::unix::fs::{symlink, PermissionsExt};

    fn fixture(root: &Path) -> (PathBuf, Value) {
        for name in ["rustc", "sdk", "driver"] {
            fs::write(root.join(name), name).unwrap();
        }
        let value = json!({"schema_version":1,"backend":"compiler","commit_hash":COMMIT,
            "rustc":root.join("rustc"),"rustc_sha256":hash(&root.join("rustc")).unwrap(),
            "sdk":root.join("sdk"),"sdk_sha256":hash(&root.join("sdk")).unwrap(),
            "driver":root.join("driver"),"driver_sha256":hash(&root.join("driver")).unwrap(),
            "protected_source_roots":[root.join("protected")]});
        let path = root.join("profile.json");
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        (path, value)
    }

    #[test]
    fn stable_profile_retains_exact_bytes_and_binds_each_build_input() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let (path, _) = fixture(root);
        let profile = Profile::load(&path).unwrap();
        assert_eq!(profile.bytes, fs::read(&path).unwrap());
        assert_eq!(profile.fingerprint, hash(&path).unwrap());
        assert_eq!(profile.rustc, root.join("rustc"));
        assert_eq!(profile.sdk, root.join("sdk"));
        assert_eq!(profile.driver, root.join("driver"));
        for name in ["rustc", "sdk", "driver"] {
            fs::write(root.join(name), "changed").unwrap();
            assert!(Profile::load(&path)
                .unwrap_err()
                .to_string()
                .contains("fingerprint mismatch"));
            fs::write(root.join(name), name).unwrap();
        }
    }

    #[test]
    fn invalid_descriptor_shape_paths_versions_and_hashes_are_refused() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let (path, value) = fixture(root);
        for (key, replacement) in [
            ("schema_version", json!(2)),
            ("backend", json!("llvm")),
            ("commit_hash", json!("other")),
            ("rustc", json!("relative")),
            ("sdk", json!("relative")),
            ("driver", json!("relative")),
            ("protected_source_roots", json!([])),
            ("protected_source_roots", json!(["relative"])),
            ("protected_source_roots", json!(["/valid/../unprotected"])),
            ("protected_source_roots", json!(vec!["/root"; 17])),
            ("driver_sha256", json!("a".repeat(63))),
            ("sdk_sha256", json!("G".repeat(64))),
            ("rustc_sha256", json!("A".repeat(64))),
            ("unexpected", json!(true)),
        ] {
            let mut changed = value.clone();
            changed[key] = replacement;
            fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
            assert!(Profile::load(&path).is_err(), "accepted {key}: {changed}");
        }
        fs::write(&path, b"not json").unwrap();
        assert!(Profile::load(&path).is_err());
        fs::write(&path, vec![b' '; 65537]).unwrap();
        assert!(Profile::load(&path)
            .unwrap_err()
            .to_string()
            .contains("64 KiB"));
        fs::remove_file(&path).unwrap();
        assert!(Profile::load(&path).is_err());
    }

    #[test]
    fn descriptor_and_build_inputs_cannot_be_symbolic_links_or_directories() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let (path, _) = fixture(root);
        let link = root.join("profile-link");
        symlink(&path, &link).unwrap();
        assert!(Profile::load(&link).is_err());
        assert!(Profile::load(root).is_err());
        for name in ["rustc", "sdk", "driver"] {
            let original = root.join(name);
            let moved = root.join(format!("{name}-original"));
            fs::rename(&original, &moved).unwrap();
            symlink(&moved, &original).unwrap();
            assert!(Profile::load(&path).is_err());
            fs::remove_file(&original).unwrap();
            fs::create_dir(&original).unwrap();
            assert!(Profile::load(&path).is_err());
            fs::remove_dir(&original).unwrap();
            fs::rename(moved, original).unwrap();
        }
    }

    #[test]
    fn doctor_checks_identity_before_execution_and_checks_the_running_version() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let (path, mut value) = fixture(root);
        let compiler = root.join("rustc");
        let version = format!(
            "rustc 1.98.1 (48a229cea 2026-09-17)\ncommit-hash: {COMMIT}\nhost: aarch64-apple-darwin\nLLVM version: 22.1.8\n"
        );
        for (status, text) in [
            (true, version.clone()),
            (false, version.clone()),
            (true, version.replace(COMMIT, "other")),
            (true, version.replace("1.98.1", "1.98.2")),
            (true, version.replace("aarch64", "x86_64")),
            (true, version.replace("22.1.8", "22.1.9")),
        ] {
            assert_eq!(
                Profile::validate_version(status, &text).is_ok(),
                status && text == version
            );
        }
        fs::write(
            &compiler,
            format!("#!/bin/sh\ncat <<'VERSION'\n{version}VERSION\n"),
        )
        .unwrap();
        fs::set_permissions(&compiler, fs::Permissions::from_mode(0o700)).unwrap();
        value["rustc_sha256"] = hash(&compiler).unwrap().into();
        let wrong_compiler = root.join("driver");
        fs::write(&wrong_compiler, "#!/bin/sh\n: > \"$0.executed\"\n").unwrap();
        fs::set_permissions(&wrong_compiler, fs::Permissions::from_mode(0o700)).unwrap();
        value["driver_sha256"] = hash(&wrong_compiler).unwrap().into();
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        let profile = Profile::load(&path).unwrap();
        profile.doctor(&compiler).unwrap();
        assert!(profile.doctor(&wrong_compiler).is_err());
        assert!(!root.join("driver.executed").exists());
        fs::write(&compiler, "changed").unwrap();
        assert!(profile.doctor(&compiler).is_err());

        fs::write(&compiler, "#!/bin/sh\nexit 1\n").unwrap();
        value["rustc_sha256"] = hash(&compiler).unwrap().into();
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        let profile = Profile::load(&path).unwrap();
        assert!(profile.doctor(&compiler).is_err());
        fs::remove_file(&compiler).unwrap();
        assert!(profile.doctor(&compiler).is_err());
    }
}
