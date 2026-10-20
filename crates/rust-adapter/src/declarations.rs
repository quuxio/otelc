//! Read original declarations and annotations; compiler hooks own eligibility and insertion.
use anyhow::{bail, Context, Result};
use proc_macro2::Span;
use quote::ToTokens;
use quux_otelc_config::Selection;
use quux_otelc_rust::policy::Plan;
use serde::Serialize;
use syn::{
    spanned::Spanned,
    visit::{self, Visit},
};
#[derive(Debug)]
pub(crate) struct Function {
    pub name: String,
    pub selected: bool,
    pub unsupported: Option<&'static str>,
    pub(crate) compiler_site: CompilerSite,
}
#[derive(Clone, Debug, serde::Deserialize, Serialize)]
pub struct CompilerSite {
    pub leaf: String,
    pub header_start: usize,
    pub header_end: usize,
    pub body_start_line: usize,
    pub body_start_column: usize,
    pub body_end_line: usize,
    pub body_end_column: usize,
    pub declaration_start_line: usize,
    pub declaration_start_column: usize,
    pub declaration_end_line: usize,
    pub declaration_end_column: usize,
    pub startup: bool,
    #[serde(skip)]
    pub asynchronous: bool,
}
impl CompilerSite {
    fn new(
        source: &str,
        leaf: String,
        declaration: (Span, Span),
        body: Span,
        startup: bool,
        asynchronous: bool,
    ) -> Self {
        let (header, declaration_end) = declaration;
        // Rust debug-info columns count bytes. proc-macro2 positions count
        // Unicode characters, including the closure's original prefix tokens.
        let column = |position: proc_macro2::LineColumn| {
            let offset = position_offset(source, position);
            offset - source[..offset].rfind('\n').map_or(0, |index| index + 1) + 1
        };
        Self {
            leaf,
            header_start: header.start().line,
            header_end: body.start().line,
            body_start_line: body.start().line,
            body_start_column: column(body.start()),
            body_end_line: body.end().line,
            body_end_column: column(body.end()),
            declaration_start_line: header.start().line,
            declaration_start_column: column(header.start()),
            declaration_end_line: declaration_end.start().line,
            declaration_end_column: column(declaration_end.start()),
            startup,
            asynchronous,
        }
    }
}
struct Parser<'a> {
    source: &'a str,
    scope: Vec<String>,
    functions: Vec<Function>,
    selection: Selection,
    selected_source: bool,
    plan: &'a Plan,
    startup: bool,
    error: Option<anyhow::Error>,
}
fn offset(source: &str, span: Span) -> usize {
    position_offset(source, span.start())
}
fn position_offset(source: &str, position: proc_macro2::LineColumn) -> usize {
    let mut lines = vec![0];
    lines.extend(
        source
            .bytes()
            .enumerate()
            .filter_map(|(index, value)| (value == b'\n').then_some(index + 1)),
    );
    let start = lines
        .get(position.line.saturating_sub(1))
        .copied()
        .unwrap_or(source.len());
    // proc-macro2 columns count UTF-8 characters; String insertion needs bytes.
    start
        + source[start..]
            .char_indices()
            .nth(position.column)
            .map(|(index, _)| index)
            .unwrap_or(source.len() - start)
}
fn annotate(
    source: &str,
    span: Span,
    attrs: &[syn::Attribute],
    enabled: bool,
) -> Result<(bool, bool)> {
    if !enabled {
        return Ok((false, false));
    }
    let mut markers = Vec::new();
    for attr in attrs {
        if let syn::Meta::NameValue(value) = &attr.meta {
            if value.path.is_ident("doc") {
                if let syn::Expr::Lit(value) = &value.value {
                    if let syn::Lit::Str(value) = &value.lit {
                        markers.push(value.value().trim().to_owned());
                    }
                }
            }
        }
        let parts: Vec<_> = attr
            .path()
            .segments
            .iter()
            .map(|segment| segment.ident.to_string())
            .collect();
        if parts.len() == 2 && parts[0] == "otelc" {
            markers.push(format!("otelc.{}", parts[1]));
        }
    }
    for (index, line) in source[..offset(source, span)].rsplit('\n').enumerate() {
        let line = line.trim();
        if line.is_empty() && index == 0 {
            continue;
        }
        if let Some(comment) = line.strip_prefix("//") {
            markers.push(comment.trim_start_matches('/').trim().to_owned());
        } else {
            break;
        }
    }
    let mut include = false;
    let mut exclude = false;
    for value in markers {
        match value.as_str() {
            "otelc.instrument" => include = true,
            "otelc.exclude" => exclude = true,
            _ if value.starts_with("otelc.") => bail!("unknown Rust instrumentation annotation"),
            _ => {}
        }
    }
    Ok((include, exclude))
}
impl Parser<'_> {
    fn closure(&mut self, node: &syn::ExprClosure, binding: Option<(&[syn::Attribute], Span)>) {
        let position = node.inputs_begin.span.start();
        let label = format!("<closure>@{}:{}", position.line, position.column + 1);
        let name = format!("{}.{}", self.scope.join("."), label);
        let enabled = self.plan.annotations.read_existing && self.selected_source;
        let annotations = annotate(self.source, node.span(), &node.attrs, enabled).and_then(
            |(include, exclude)| {
                if let Some((attrs, span)) = binding {
                    let (bound_include, bound_exclude) =
                        annotate(self.source, span, attrs, enabled)?;
                    Ok((include || bound_include, exclude || bound_exclude))
                } else {
                    Ok((include, exclude))
                }
            },
        );
        let (include, exclude) = match annotations {
            Ok(value) => value,
            Err(error) => {
                self.error = Some(error);
                return;
            }
        };
        let selected = self.selected_source
            && !exclude
            && self.selection.accepts_with_annotation(&name, include);
        let unsupported = if node.constness.is_some() {
            Some("const closures cannot contain runtime probes")
        } else if node.asyncness.is_some() {
            Some("async closures require lending-future and capture qualification")
        } else if node.modifiers.require_empty().is_err() {
            Some("closure modifiers require separate compiler qualification")
        } else {
            None
        };
        self.functions.push(Function {
            name,
            selected,
            unsupported,
            compiler_site: CompilerSite::new(
                self.source,
                "<closure>".into(),
                (node.span(), node.inputs_begin.span),
                node.body.span(),
                false,
                node.asyncness.is_some(),
            ),
        });
        self.scope.push(label);
        visit::visit_expr_closure(self, node);
        self.scope.pop();
    }
    fn function(
        &mut self,
        sig: &syn::Signature,
        body: &syn::Block,
        attrs: &[syn::Attribute],
        span: Span,
        root: bool,
    ) {
        let name = format!("{}.{}", self.scope.join("."), sig.ident);
        let annotations = annotate(
            self.source,
            span,
            attrs,
            self.plan.annotations.read_existing && self.selected_source,
        );
        let (include, exclude) = match annotations {
            Ok(value) => value,
            Err(error) => {
                self.error = Some(error);
                return;
            }
        };
        let selected = self.selected_source
            && !exclude
            && self.selection.accepts_with_annotation(&name, include);
        let unsupported = if sig.constness.is_some() {
            Some("const functions cannot contain runtime probes")
        } else if sig.asyncness.is_some() && sig.inputs.iter().any(|input| matches!(input, syn::FnArg::Typed(input) if !matches!(&*input.pat, syn::Pat::Ident(binding) if binding.by_ref.is_none() && binding.subpat.is_none()))) {
            Some("async destructured/ref parameters require compiler lifetime qualification")
        } else if attrs.iter().any(|attr| {
            attr.path().is_ident("naked")
                || (attr.path().is_ident("unsafe")
                    && attr
                        .parse_args::<syn::Path>()
                        .is_ok_and(|path| path.is_ident("naked")))
        }) {
            Some("naked functions cannot contain body probes")
        } else {
            None
        };
        self.functions.push(Function {
            name,
            selected,
            unsupported,
            compiler_site: CompilerSite::new(
                self.source,
                sig.ident.to_string(),
                (span, sig.fn_token.span),
                body.span(),
                self.startup && root && sig.ident == "main",
                sig.asyncness.is_some(),
            ),
        });
    }
}
impl<'ast> Visit<'ast> for Parser<'_> {
    fn visit_local(&mut self, node: &'ast syn::Local) {
        if let Some(initializer) = &node.init {
            if let syn::Expr::Closure(closure) = &*initializer.expr {
                self.closure(closure, Some((&node.attrs, node.span())));
                return;
            }
        }
        visit::visit_local(self, node);
    }
    fn visit_expr_closure(&mut self, node: &'ast syn::ExprClosure) {
        self.closure(node, None);
    }
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        let root = self.scope.len() == 1;
        self.function(&node.sig, &node.block, &node.attrs, node.span(), root);
        self.scope.push(node.sig.ident.to_string());
        visit::visit_item_fn(self, node);
        self.scope.pop();
    }
    fn visit_item_mod(&mut self, node: &'ast syn::ItemMod) {
        self.scope.push(node.ident.to_string());
        visit::visit_item_mod(self, node);
        self.scope.pop();
    }
    fn visit_item_impl(&mut self, node: &'ast syn::ItemImpl) {
        // Keep the complete receiver type: separate specialised implementations
        // and qualified types must not silently share a metric identity.
        let name = node.self_ty.to_token_stream().to_string();
        let name = if let Some((path, _)) = &node.trait_ {
            format!("<{name} as {}>", path.to_token_stream())
        } else {
            name
        };
        self.scope.push(name);
        visit::visit_item_impl(self, node);
        self.scope.pop();
    }
    fn visit_impl_item_fn(&mut self, node: &'ast syn::ImplItemFn) {
        self.function(&node.sig, &node.block, &node.attrs, node.span(), false);
        self.scope.push(node.sig.ident.to_string());
        visit::visit_impl_item_fn(self, node);
        self.scope.pop();
    }
    fn visit_item_trait(&mut self, node: &'ast syn::ItemTrait) {
        self.scope.push(node.ident.to_string());
        visit::visit_item_trait(self, node);
        self.scope.pop();
    }
    fn visit_trait_item_fn(&mut self, node: &'ast syn::TraitItemFn) {
        if let Some(body) = &node.default {
            self.function(&node.sig, body, &node.attrs, node.span(), false);
        }
        self.scope.push(node.sig.ident.to_string());
        visit::visit_trait_item_fn(self, node);
        self.scope.pop();
    }
}

pub(crate) fn parse(
    source: &str,
    identity: &str,
    plan: &Plan,
    selected_source: bool,
    startup: bool,
) -> Result<Vec<Function>> {
    let parsed = syn::parse_file(source).context("parse original Rust source")?;
    let mut parser = Parser {
        source,
        scope: vec![identity.into()],
        functions: vec![],
        selection: Selection::new(&plan.functions.include, &plan.functions.exclude, false)?,
        selected_source,
        plan,
        startup,
        error: None,
    };
    parser.visit_file(&parsed);
    if let Some(error) = parser.error {
        return Err(error);
    }
    Ok(parser.functions)
}
