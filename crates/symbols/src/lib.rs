//! Executable identity and retained function inventory; no symbol work in probes.
use anyhow::{bail, Context, Result};
use object::{Object, ObjectSection, ObjectSymbol, RelocationTarget, SymbolKind};
use quux_otelc_config::Config;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Function {
    pub address: u64,
    pub linkage_name: String,
    pub display_name: String,
    pub selected: bool,
    #[serde(default)]
    pub annotated: bool,
    pub reason: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub schema_version: u32,
    pub backend: String,
    pub image_id: String,
    pub architecture: String,
    pub compiler: String,
    pub functions: Vec<Function>,
}
fn identity(file: &object::File<'_>) -> Result<String> {
    let bytes = if let Some(uuid) = file.mach_uuid()? {
        uuid.to_vec()
    } else {
        file.build_id()?
            .context("executable has no build ID; link Linux executables with --build-id")?
            .to_vec()
    };
    Ok(bytes.iter().map(|v| format!("{v:02x}")).collect())
}
pub fn image_info(path: &Path) -> Result<(String, String)> {
    let bytes = std::fs::read(path).context("read executable")?;
    let file = object::File::parse(bytes.as_slice()).context("parse executable")?;
    Ok((identity(&file)?, format!("{:?}", file.architecture())))
}
pub fn manifest_path(binary: &Path) -> PathBuf {
    let mut path = binary.as_os_str().to_os_string();
    path.push(".otelc.json");
    path.into()
}
pub fn create(
    binary: &Path,
    config: &Config,
    compiler: String,
    probed: &Probes,
) -> Result<Manifest> {
    let bytes = std::fs::read(binary)?;
    let file = object::File::parse(bytes.as_slice())?;
    let selection = config.function_selection()?;
    let mut functions = Vec::new();
    for symbol in file
        .symbols()
        .filter(|s| s.kind() == SymbolKind::Text && s.is_definition() && s.address() != 0)
    {
        let Ok(raw) = symbol.name() else { continue };
        let linkage = if file.format() == object::BinaryFormat::MachO {
            raw.strip_prefix('_').unwrap_or(raw)
        } else {
            raw
        };
        let display = cpp_demangle::Symbol::new(linkage)
            .ok()
            .and_then(|s| s.demangle().ok())
            .unwrap_or_else(|| linkage.into());
        let annotated = probed.annotated.contains(linkage);
        let selected = probed.functions.contains(linkage)
            && selection.accepts_with_annotation(&display, config.read_annotations && annotated);
        functions.push(Function {
            address: symbol.address(),
            linkage_name: linkage.into(),
            display_name: display.clone(),
            selected,
            annotated,
            reason: if !probed.functions.contains(linkage) {
                "no compiled probes"
            } else if selected && config.read_annotations && annotated {
                "included by annotation"
            } else {
                selection.reason(&display)
            }
            .into(),
        });
    }
    functions.sort_by_key(|f| f.address);
    functions.dedup_by(|a, b| a.address == b.address && a.linkage_name == b.linkage_name);
    if functions
        .windows(2)
        .any(|f| f[0].address == f[1].address && (f[0].selected || f[1].selected))
    {
        bail!("selected symbols share an address; folded/aliased functions are unsupported");
    }
    functions.dedup_by(|a, b| a.address == b.address);
    let mut selected_names = std::collections::HashSet::new();
    if functions
        .iter()
        .filter(|f| f.selected)
        .any(|f| !selected_names.insert(&f.display_name))
    {
        bail!("selected function names are ambiguous across compilation units");
    }
    if functions.iter().filter(|f| f.selected).count() > config.runtime.max_functions {
        bail!("selected function count exceeds max_functions");
    }
    let manifest = Manifest {
        schema_version: 1,
        backend: config.build.backend.clone(),
        image_id: identity(&file)?,
        architecture: format!("{:?}", file.architecture()),
        compiler,
        functions,
    };
    manifest.save(&manifest_path(binary))?;
    Ok(manifest)
}
impl Manifest {
    pub fn load(path: &Path) -> Result<Self> {
        let metadata = std::fs::metadata(path)?;
        if metadata.len() > 64 * 1024 * 1024 {
            bail!("manifest exceeds 64 MiB");
        }
        let value: Self =
            serde_json::from_slice(&std::fs::read(path)?).context("invalid manifest")?;
        if value.schema_version != 1
            || !matches!(value.backend.as_str(), "callbacks" | "llvm")
            || value.functions.len() > 1000000
        {
            bail!("unsupported manifest");
        }
        if value
            .functions
            .windows(2)
            .any(|f| f[0].address >= f[1].address)
            || value.functions.iter().any(|f| {
                f.address == 0 || f.display_name.len() > 4096 || f.linkage_name.len() > 4096
            })
        {
            bail!("invalid or ambiguous function inventory");
        }
        Ok(value)
    }
    pub fn verify(&self, binary: &Path) -> Result<()> {
        let (id, architecture) = image_info(binary)?;
        if self.image_id != id || self.architecture != architecture {
            bail!("manifest does not match executable identity/architecture");
        }
        Ok(())
    }
    pub fn save(&self, path: &Path) -> Result<()> {
        std::fs::write(path, serde_json::to_vec_pretty(self)?)?;
        Ok(())
    }
}
#[cfg(test)]
#[path = "tests/symbols.rs"]
mod tests;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ObjectMarker {
    digest: String,
    backend: String,
    functions: Vec<String>,
    #[serde(default)]
    annotated: Vec<String>,
}
#[derive(Default)]
pub struct Probes {
    pub functions: std::collections::HashSet<String>,
    pub annotated: std::collections::HashSet<String>,
}
impl Probes {
    pub fn extend(&mut self, other: Self) {
        self.functions.extend(other.functions);
        self.annotated.extend(other.annotated);
    }
}
struct ObjectInventory {
    digest: String,
    functions: Vec<String>,
    annotated: Vec<String>,
    hooks: bool,
    llvm_inventory: bool,
}
fn object_marker(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".otelc-object.json");
    value.into()
}
fn object_inventory(path: &Path) -> Result<ObjectInventory> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path)?;
    let file = object::File::parse(bytes.as_slice())?;
    let mut functions = Vec::new();
    let mut hooks = false;
    let mut annotated = Vec::new();
    for symbol in file.symbols() {
        let Ok(raw) = symbol.name() else { continue };
        let name = if file.format() == object::BinaryFormat::MachO {
            raw.strip_prefix('_').unwrap_or(raw)
        } else {
            raw
        };
        if name.starts_with("__cyg_profile_func_") || name.starts_with("otelc_function_") {
            if symbol.is_definition() {
                bail!("input defines conflicting instrumentation callbacks");
            }
            hooks = true;
        }
        if symbol.is_definition() && symbol.kind() == SymbolKind::Text {
            functions.push(name.to_string());
        }
    }
    let mut llvm_inventory = false;
    // The LLVM pass retains an exact relocation inventory. Code generated
    // after the pass does not become selected merely because it has a symbol.
    for section in file.sections() {
        if matches!(
            section.name().unwrap_or(""),
            "__otelc" | ".otelc" | "__otela" | ".otela"
        ) {
            let destination = if matches!(section.name().unwrap_or(""), "__otela" | ".otela") {
                &mut annotated
            } else {
                llvm_inventory = true;
                functions.clear();
                &mut functions
            };
            for (offset, relocation) in section.relocations() {
                if let RelocationTarget::Symbol(index) = relocation.target() {
                    let raw = file.symbol_by_index(index)?.name()?;
                    destination.push(
                        if file.format() == object::BinaryFormat::MachO {
                            raw.strip_prefix('_').unwrap_or(raw)
                        } else {
                            raw
                        }
                        .into(),
                    );
                } else if let RelocationTarget::Section(index) = relocation.target() {
                    let target = file.section_by_index(index)?;
                    let data = section.data()?;
                    let offset = offset as usize;
                    let bytes: [u8; 8] = data
                        .get(offset..offset + 8)
                        .context("invalid probe relocation")?
                        .try_into()?;
                    let implicit = if file.is_little_endian() {
                        u64::from_le_bytes(bytes)
                    } else {
                        u64::from_be_bytes(bytes)
                    };
                    let address = (target.address() as i128
                        + implicit as i128
                        + relocation.addend() as i128) as u64;
                    for symbol in file.symbols().filter(|s| {
                        s.section_index() == Some(index)
                            && s.address() == address
                            && s.kind() == SymbolKind::Text
                    }) {
                        let raw = symbol.name()?;
                        destination.push(raw.strip_prefix('_').unwrap_or(raw).into());
                    }
                }
            }
        }
    }
    Ok(ObjectInventory {
        digest: format!("{:x}", Sha256::digest(&bytes)),
        functions,
        annotated,
        hooks,
        llvm_inventory,
    })
}
pub fn mark_object(path: &Path, instrumented: bool, backend: &str) -> Result<Probes> {
    let inventory = object_inventory(path)?;
    let functions = if instrumented && (backend != "llvm" || inventory.llvm_inventory) {
        inventory.functions
    } else {
        vec![]
    };
    let annotated: Vec<_> = inventory
        .annotated
        .into_iter()
        .filter(|name| functions.contains(name))
        .collect();
    std::fs::write(
        object_marker(path),
        serde_json::to_vec(&ObjectMarker {
            digest: inventory.digest,
            backend: backend.into(),
            functions: functions.clone(),
            annotated: annotated.clone(),
        })?,
    )?;
    Ok(Probes {
        functions: functions.into_iter().collect(),
        annotated: annotated.into_iter().collect(),
    })
}
pub fn object_probes(path: &Path, backend: &str) -> Result<Probes> {
    let inventory = object_inventory(path)?;
    let marker = object_marker(path);
    if !marker.exists() {
        if inventory.hooks {
            bail!("instrumented object has no otelc build marker");
        }
        return Ok(Probes::default());
    }
    let value: ObjectMarker = serde_json::from_slice(&std::fs::read(marker)?)?;
    if !value.functions.is_empty() && value.backend != backend {
        bail!("object instrumentation backend does not match link configuration");
    }
    if value.digest != inventory.digest {
        bail!("object does not match its otelc build marker");
    }
    if value
        .annotated
        .iter()
        .any(|name| !value.functions.contains(name))
    {
        bail!("annotation metadata has no compiled probes");
    }
    Ok(Probes {
        functions: value.functions.into_iter().collect(),
        annotated: value.annotated.into_iter().collect(),
    })
}
