//! Validate a single object-level main rename without recompiling application code.
use super::*;
use object::{ObjectComdat, RelocationTarget, SectionFlags};
use sha2::{Digest, Sha256};

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MainRenameRecord {
    operation: String,
    source_digest: String,
    source_marker_digest: String,
    tool_digest: String,
    tool_version: String,
    target: String,
    original_functions: Vec<String>,
    original_annotated: Vec<String>,
}

pub fn valid_target(value: &str) -> bool {
    value != "main"
        && !value.is_empty()
        && value.len() <= 127
        && value.bytes().enumerate().all(|(index, byte)| {
            byte == b'_' || byte.is_ascii_alphabetic() || index > 0 && byte.is_ascii_digit()
        })
}
fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn renamed(values: &[String], target: &str) -> Vec<String> {
    values
        .iter()
        .map(|name| {
            if name == "main" {
                target.to_owned()
            } else {
                name.clone()
            }
        })
        .collect()
}
pub(super) fn marker_renames(
    marker: &ObjectMarker,
) -> Result<std::collections::BTreeMap<String, String>> {
    let mut aliases = std::collections::BTreeMap::new();
    if let Some(record) = &marker.transformation {
        if record.operation != "rename-main-v1"
            || !valid_target(&record.target)
            || !valid_digest(&record.source_digest)
            || !valid_digest(&record.source_marker_digest)
            || !valid_digest(&record.tool_digest)
            || record.tool_version.is_empty()
            || record.tool_version.len() > 4096
            || marker.functions != renamed(&record.original_functions, &record.target)
            || marker.annotated != renamed(&record.original_annotated, &record.target)
            || record
                .original_functions
                .iter()
                .any(|name| name == &record.target)
            || record
                .original_annotated
                .iter()
                .any(|name| !record.original_functions.contains(name))
        {
            bail!("invalid object main rename provenance");
        }
        if record.original_functions.iter().any(|name| name == "main") {
            aliases.insert(record.target.clone(), "main".into());
        }
    }
    Ok(aliases)
}

pub struct MainRename {
    bytes: Vec<u8>,
    marker: ObjectMarker,
    marker_digest: String,
    target: String,
    raw_main: Vec<u8>,
    raw_target: Vec<u8>,
}
impl MainRename {
    /// Snapshot the original bytes/marker before any external tool can read them.
    pub fn load(path: &Path, target: &str) -> Result<Self> {
        if !valid_target(target) {
            bail!("main target must be a distinct C identifier of at most 127 bytes");
        }
        let bytes = std::fs::read(path)?;
        let marker_bytes = std::fs::read(object_marker(path))
            .context("main rename requires the original otelc object marker")?;
        let marker: ObjectMarker = serde_json::from_slice(&marker_bytes)?;
        let inventory = object_inventory_bytes(&bytes)?;
        validated_probes(&inventory, &marker, &marker.backend)?;
        if marker.transformation.is_some() {
            bail!("chained object transforms are unsupported");
        }
        if !matches!(marker.backend.as_str(), "llvm" | "callbacks")
            || marker
                .functions
                .iter()
                .any(|name| !inventory.functions.contains(name))
        {
            bail!("object marker does not describe compiled native probes");
        }
        let file = object::File::parse(bytes.as_slice())?;
        if file.kind() != object::ObjectKind::Relocatable
            || !matches!(
                file.format(),
                object::BinaryFormat::MachO | object::BinaryFormat::Elf
            )
        {
            bail!("main rename requires a relocatable Mach-O or ELF object");
        }
        let prefix = if file.format() == object::BinaryFormat::MachO {
            "_"
        } else {
            ""
        };
        let raw_main = format!("{prefix}main").into_bytes();
        let raw_target = format!("{prefix}{target}").into_bytes();
        if file
            .symbols()
            .any(|symbol| symbol.name_bytes().ok() == Some(raw_target.as_slice()))
        {
            bail!("main rename target already exists in the object");
        }
        Ok(Self {
            bytes,
            marker,
            marker_digest: digest(&marker_bytes),
            target: target.into(),
            raw_main,
            raw_target,
        })
    }
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
    pub fn redefine_argument(&self) -> String {
        format!(
            "{}={}",
            String::from_utf8_lossy(&self.raw_main),
            String::from_utf8_lossy(&self.raw_target)
        )
    }
    /// Validate code/data, symbol attributes and normalized relocations before marking.
    pub fn marker(&self, transformed: &[u8], tool: &[u8], version: &str) -> Result<Vec<u8>> {
        if shape(&self.bytes, Some((&self.raw_main, &self.raw_target)))?
            != shape(transformed, None)?
        {
            bail!("object tool changed content beyond the declared main-symbol rename");
        }
        let inventory = object_inventory_bytes(transformed)?;
        let functions = renamed(&self.marker.functions, &self.target);
        let annotated = renamed(&self.marker.annotated, &self.target);
        if functions
            .iter()
            .any(|name| !inventory.functions.contains(name))
        {
            bail!("transformed object lost compiled probes");
        }
        let marker = ObjectMarker {
            digest: inventory.digest,
            backend: self.marker.backend.clone(),
            functions,
            annotated,
            transformation: Some(MainRenameRecord {
                operation: "rename-main-v1".into(),
                source_digest: self.marker.digest.clone(),
                source_marker_digest: self.marker_digest.clone(),
                tool_digest: digest(tool),
                tool_version: version.into(),
                target: self.target.clone(),
                original_functions: self.marker.functions.clone(),
                original_annotated: self.marker.annotated.clone(),
            }),
        };
        marker_renames(&marker)?;
        Ok(serde_json::to_vec(&marker)?)
    }
}

// Symbol/string/relocation tables may be re-encoded by objcopy. Compare their
// decoded identities; all ordinary section bytes (including code/debug data) stay exact.
fn shape(bytes: &[u8], rename: Option<(&[u8], &[u8])>) -> Result<Vec<String>> {
    let file = object::File::parse(bytes)?;
    let name = |raw: &[u8]| -> Vec<u8> {
        match rename {
            Some((before, after)) if raw == before => after.to_vec(),
            _ => raw.to_vec(),
        }
    };
    let section_name = |index| -> Result<String> {
        let section = file.section_by_index(index)?;
        Ok(format!(
            "{:?} {:?} {:?} {}",
            index,
            section.segment_name_bytes()?,
            section.name_bytes()?,
            section.address()
        ))
    };
    let mut entries = vec![format!(
        "header {:?} {:?} {:?} {} {} {:?}",
        file.format(),
        file.architecture(),
        file.kind(),
        file.is_little_endian(),
        file.is_64(),
        file.flags()
    )];
    if file.format() == object::BinaryFormat::MachO {
        entries.push(format!(
            "cpu {:?}",
            bytes.get(4..12).context("truncated Mach-O header")?
        ));
    }
    entries.push(format!("entry {}", file.entry()));
    for section in file.sections() {
        let structural = matches!(section.flags(), SectionFlags::Elf { sh_type, .. }
            if [object::elf::SHT_SYMTAB, object::elf::SHT_STRTAB, object::elf::SHT_REL,
                object::elf::SHT_RELA, object::elf::SHT_GROUP].contains(&sh_type));
        let data = if structural {
            None
        } else {
            Some((section.size(), digest(section.data()?)))
        };
        entries.push(format!(
            "section {:?} {:?} {} {} {:?} {:?} {:?}",
            section_name(section.index())?,
            section.segment_name_bytes()?,
            section.address(),
            section.align(),
            section.kind(),
            section.flags(),
            data
        ));
        for (offset, relocation) in section.relocations() {
            let target = match relocation.target() {
                RelocationTarget::Symbol(index) => format!(
                    "symbol {:?}",
                    name(file.symbol_by_index(index)?.name_bytes()?)
                ),
                RelocationTarget::Section(index) => format!("section {:?}", section_name(index)?),
                other => format!("{other:?}"),
            };
            entries.push(format!(
                "relocation {:?} {} {} {:?} {:?} {} {} {:?} {target}",
                section.name_bytes()?,
                offset,
                relocation.size(),
                relocation.kind(),
                relocation.encoding(),
                relocation.addend(),
                relocation.has_implicit_addend(),
                relocation.flags()
            ));
        }
    }
    for symbol in file.symbols() {
        let section = match symbol.section() {
            object::SymbolSection::Section(index) => format!("section {:?}", section_name(index)?),
            other => format!("{other:?}"),
        };
        entries.push(format!(
            "symbol {:?} {} {} {:?} {:?} {} {:?} {section}",
            name(symbol.name_bytes()?),
            symbol.address(),
            symbol.size(),
            symbol.kind(),
            symbol.scope(),
            symbol.is_weak(),
            symbol.flags()
        ));
    }
    for group in file.comdats() {
        let sections = group
            .sections()
            .map(section_name)
            .collect::<Result<Vec<_>>>()?;
        entries.push(format!(
            "group {:?} {:?} {:?}",
            name(group.name_bytes()?),
            group.kind(),
            sections
        ));
    }
    entries.sort();
    Ok(entries)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn fixture() -> (tempfile::TempDir, PathBuf) {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(
            root.path().join("input.c"),
            "int main(void){return 5;} int other(void){return 7;}",
        )
        .unwrap();
        let output = Command::new("clang")
            .args(["-O0", "-c", "input.c", "-o", "original.o"])
            .current_dir(root.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let path = root.path().join("original.o");
        mark_object(&path, false, "llvm").unwrap();
        (root, path)
    }

    #[test]
    fn renamed_string_only_is_valid_but_code_changes_and_missing_rename_are_not() {
        let (_root, path) = fixture();
        let snapshot = MainRename::load(&path, "swap").unwrap();
        assert!(snapshot
            .marker(snapshot.bytes(), b"tool", "LLVM version 22.1.8")
            .is_err());
        let original = object::File::parse(snapshot.bytes()).unwrap();
        let raw = original
            .symbols()
            .find_map(|symbol| {
                let name = symbol.name_bytes().unwrap();
                (name == snapshot.raw_main).then_some(name)
            })
            .unwrap();
        let start = raw.as_ptr() as usize - snapshot.bytes().as_ptr() as usize;
        let mut renamed = snapshot.bytes().to_vec();
        renamed[start..start + raw.len()].copy_from_slice(&snapshot.raw_target);
        let marker = snapshot
            .marker(&renamed, b"tool", "LLVM version 22.1.8")
            .unwrap();
        let marker: ObjectMarker = serde_json::from_slice(&marker).unwrap();
        assert!(marker_renames(&marker).unwrap().is_empty());
        assert!(snapshot.marker(&renamed, b"tool", "").is_err());
        let file = object::File::parse(renamed.as_slice()).unwrap();
        let (offset, size) = file
            .sections()
            .find(|section| section.kind() == object::SectionKind::Text)
            .unwrap()
            .file_range()
            .unwrap();
        assert!(size > 0);
        renamed[offset as usize] ^= 1;
        assert!(snapshot
            .marker(&renamed, b"tool", "LLVM version 22.1.8")
            .is_err());
        assert!(snapshot.marker(b"not an object", b"tool", "v").is_err());
    }

    #[test]
    fn malformed_backend_probe_claim_and_nonrelocatable_inputs_are_refused() {
        let (root, path) = fixture();
        let marker_path = object_marker(&path);
        let original = std::fs::read(&marker_path).unwrap();
        for change in ["backend", "functions", "annotated"] {
            let mut marker: serde_json::Value = serde_json::from_slice(&original).unwrap();
            marker[change] = if change == "backend" {
                "unknown".into()
            } else {
                serde_json::json!(["absent"])
            };
            std::fs::write(&marker_path, serde_json::to_vec(&marker).unwrap()).unwrap();
            assert!(MainRename::load(&path, "swap").is_err());
        }
        std::fs::write(&marker_path, original).unwrap();
        let output = Command::new("clang")
            .args(["input.c", "-o", "app"])
            .current_dir(root.path())
            .output()
            .unwrap();
        assert!(output.status.success());
        let app = root.path().join("app");
        mark_object(&app, false, "llvm").unwrap();
        assert!(MainRename::load(&app, "swap").is_err());
        for target in ["", "main", "bad-name", "0name", "other", "\u{e9}"] {
            assert!(MainRename::load(&path, target).is_err());
        }
        assert!(!valid_target(&"x".repeat(128)));
        assert!(valid_target("_new0"));
    }
}
