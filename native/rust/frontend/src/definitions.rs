//! Shared compiler-owned source identities for the qualified compiler drivers.
use anyhow::{Context, Result, bail};
use quux_otelc_config::Selection;
use quux_otelc_rust_adapter::{
    compiler_frontend::{Request, position},
    compiler_inventory::Definition,
    transform::CompilerSite,
};
use rustc_hir::def::DefKind;
use rustc_middle::ty::TyCtxt;
use rustc_span::{Span, def_id::LocalDefId};
use sha2::{Digest, Sha256};
use std::{fs, path::PathBuf};

pub(crate) struct Definitions<'a> {
    pub(crate) request: &'a Request,
    pub(crate) runtime_futures: bool,
}
impl Definitions<'_> {
    fn source_file(&self, file: PathBuf) -> Result<Option<PathBuf>> {
        let Ok(canonical) = file.canonicalize() else {
            return Ok(None);
        };
        if self
            .request
            .protected_source_roots
            .iter()
            .any(|path| canonical.starts_with(path))
        {
            return Ok(None);
        }
        let logical = std::path::absolute(file)?;
        // Distinct module aliases can refer to the same read-only physical
        // source. Keep their original configured identities while validating
        // the physical path against SDK exclusions and compiler source hashes.
        let file = if self.request.policy.backend == "compiler"
            && logical.starts_with(&self.request.root)
            && self.request.sources.contains_key(&logical)
        {
            logical
        } else {
            canonical
        };
        Ok(Some(file))
    }
    fn name(tcx: TyCtxt<'_>, id: LocalDefId) -> String {
        tcx.def_path(id.to_def_id())
            .data
            .iter()
            .map(|component| {
                component
                    .data
                    .get_opt_name()
                    .map(|name| name.to_string())
                    .unwrap_or_else(|| {
                        format!(
                            "<{}#{}>",
                            format!("{:?}", component.data).to_ascii_lowercase(),
                            component.disambiguator
                        )
                    })
            })
            .collect::<Vec<_>>()
            .join(".")
    }
    fn original(&self, tcx: TyCtxt<'_>, id: LocalDefId) -> Result<Option<Definition>> {
        let map = tcx.sess.source_map();
        let span = tcx.def_span(id);
        let location = map.lookup_char_pos(span.lo());
        let file = PathBuf::from(
            location
                .file
                .name
                .prefer_local_unconditionally()
                .to_string(),
        );
        let Some(file) = self.source_file(file)? else {
            return Ok(None);
        };
        if !self.request.sources.contains_key(&file)
            || !file.starts_with(&self.request.root)
            || self
                .request
                .protected_source_roots
                .iter()
                .any(|path| file.starts_with(path))
        {
            return Ok(None);
        }
        let source = fs::read_to_string(&file)?;
        if Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
            != self.request.sources[&file]
            || !location.file.src_hash.matches(&source)
        {
            bail!("original Rust source differs from the compiler's loaded input");
        }
        let at = |position_| {
            position(
                &source,
                location.file.original_relative_byte_pos(position_).0 as usize,
            )
        };
        // An expression-bodied closure can lower directly into a standard
        // library macro's source. Its original declaration, not that expanded
        // expression, already identifies the parsed closure unambiguously.
        if tcx.def_kind(id) == DefKind::Closure {
            return self.request.original(
                file.to_str().context("Rust frontend path must be UTF-8")?,
                ((0, 0), (0, 0)),
                at(span.lo())?,
                true,
            );
        }
        let body = tcx.hir_body_owned_by(id).value.span;
        if [span.hi(), body.lo(), body.hi()].iter().any(|position| {
            map.lookup_char_pos(*position).file.start_pos != location.file.start_pos
        }) {
            return Ok(None);
        }
        self.request.original(
            file.to_str().context("Rust frontend path must be UTF-8")?,
            (at(body.lo())?, at(body.hi())?),
            at(span.lo())?,
            false,
        )
    }

    fn qualified(
        runtime_futures: bool,
        tcx: TyCtxt<'_>,
        id: LocalDefId,
        mut definition: Definition,
        expanded: bool,
    ) -> Result<Option<(Definition, bool)>> {
        if definition.unsupported == "const functions cannot contain runtime probes" {
            // CTFE has already finished. Only emitted runtime bodies receive IR
            // probes; no SDK call is inserted into Rust's const evaluator.
            definition.unsupported.clear();
        }
        definition.site.startup = tcx
            .entry_fn(())
            .is_some_and(|entry| entry.0 == id.to_def_id());
        if !runtime_futures
            && (tcx.asyncness(id.to_def_id()).is_async()
                || tcx.coroutine_kind(id.to_def_id()).is_some())
        {
            definition.unsupported =
                "async compiler bodies require future-completion qualification".into();
        }
        if runtime_futures
            && definition.unsupported
                == "async compiler bodies require future-completion qualification"
        {
            if definition.site.startup {
                definition.unsupported =
                    "async main executors require separate qualification".into();
            } else if !tcx.asyncness(id.to_def_id()).is_async() {
                definition.unsupported =
                    "lowered async interfaces require separate completion qualification".into();
            } else {
                definition.unsupported.clear();
            }
        }
        if (definition.selected || definition.site.startup)
            && (!definition.unsupported.is_empty() || definition.name.len() > 1024)
        {
            bail!(
                "{}: unsupported compiler definition {}: {}",
                if runtime_futures {
                    "Rust compiler"
                } else {
                    "Rust LLVM"
                },
                definition.name,
                definition.unsupported
            );
        }
        Ok(Some((definition, expanded)))
    }

    pub(crate) fn definition(
        &self,
        tcx: TyCtxt<'_>,
        id: LocalDefId,
    ) -> Result<Option<(Definition, bool)>> {
        let request = &self.request;
        let original_span = tcx.def_span(id);
        let expanded = original_span.from_expansion();
        // include! and retained attribute expansions can still map exactly to
        // an original declaration. Preserve its configured name and annotations.
        if let Some(original) = self.original(tcx, id)? {
            return Self::qualified(self.runtime_futures, tcx, id, original, expanded);
        }
        let span = if expanded {
            original_span.source_callsite()
        } else {
            original_span
        };
        let map = tcx.sess.source_map();
        let location = map.lookup_char_pos(span.lo());
        let file = PathBuf::from(
            location
                .file
                .name
                .prefer_local_unconditionally()
                .to_string(),
        );
        let Some(file) = self.source_file(file)? else {
            return Ok(None);
        };
        if !request.sources.contains_key(&file)
            || !file.starts_with(&request.root)
            || request
                .protected_source_roots
                .iter()
                .any(|path| file.starts_with(path))
            || file.extension().is_none_or(|extension| extension != "rs")
        {
            return Ok(None);
        }
        let source = fs::read_to_string(&file)?;
        if Sha256::digest(source.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
            != request.sources[&file]
            || !location.file.src_hash.matches(&source)
        {
            bail!("original Rust source differs from the compiler's loaded input");
        }
        let coordinates = |span: Span| -> Result<((usize, usize), (usize, usize))> {
            let start = map.lookup_char_pos(span.lo());
            let end = map.lookup_char_pos(span.hi());
            if start.file.start_pos != location.file.start_pos
                || end.file.start_pos != location.file.start_pos
            {
                bail!(
                    "Rust compiler declaration spans multiple physical inputs: {} expanded={expanded} declaration={} start={} end={}",
                    tcx.def_path_str(id),
                    location.file.name.prefer_local_unconditionally(),
                    start.file.name.prefer_local_unconditionally(),
                    end.file.name.prefer_local_unconditionally()
                );
            }
            Ok((
                position(
                    &source,
                    location.file.original_relative_byte_pos(span.lo()).0 as usize,
                )?,
                position(
                    &source,
                    location.file.original_relative_byte_pos(span.hi()).0 as usize,
                )?,
            ))
        };
        let declaration = coordinates(span)?;
        let body_span = tcx.hir_body_owned_by(id).value.span;
        // Body expansion is independent of declaration expansion: e.g. an
        // ordinary `|| eprintln!(...)` owns a macro-expanded expression.
        let body = coordinates(body_span.source_callsite())?;
        let closure = tcx.def_kind(id) == DefKind::Closure;
        let original = if expanded {
            None
        } else {
            request.original(
                file.to_str()
                    .context("Rust frontend source path must be UTF-8")?,
                body,
                declaration.0,
                closure,
            )?
        };
        let startup = tcx
            .entry_fn(())
            .is_some_and(|entry| entry.0 == id.to_def_id());
        let definition = if let Some(original) = original {
            original
        } else {
            let relative = file.strip_prefix(&request.root)?;
            let source_selected = Selection::new(
                &request.policy.sources.include,
                &request.policy.sources.exclude,
                true,
            )?
            .accepts(
                relative
                    .to_str()
                    .context("Rust frontend source path must be UTF-8")?,
            );
            let prefix = request
                .entry
                .strip_prefix(&request.root)?
                .with_extension("");
            let name = format!(
                "{}.{}",
                prefix.to_string_lossy().replace(['/', '\\'], "."),
                Self::name(tcx, id)
            );
            let selected = source_selected
                && Selection::new(
                    &request.policy.functions.include,
                    &request.policy.functions.exclude,
                    false,
                )?
                .accepts(&name);
            Definition {
                file: file
                    .to_str()
                    .context("Rust frontend path must be UTF-8")?
                    .into(),
                name,
                suffix: tcx.def_path_str(id),
                unsupported: String::new(),
                selected,
                site: CompilerSite {
                    leaf: if closure {
                        "<closure>".into()
                    } else {
                        tcx.item_name(id.to_def_id()).to_string()
                    },
                    header_start: declaration.0.0,
                    header_end: declaration.1.0,
                    body_start_line: body.0.0,
                    body_start_column: body.0.1,
                    body_end_line: body.1.0,
                    body_end_column: body.1.1,
                    declaration_start_line: declaration.0.0,
                    declaration_start_column: declaration.0.1,
                    declaration_end_line: declaration.1.0,
                    declaration_end_column: declaration.1.1,
                    startup,
                    asynchronous: false,
                },
            }
        };
        Self::qualified(self.runtime_futures, tcx, id, definition, expanded)
    }
}
