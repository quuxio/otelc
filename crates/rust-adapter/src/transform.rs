use anyhow::{bail, Context, Result};
use proc_macro2::{Span, TokenStream, TokenTree};
use quote::ToTokens;
use quux_otelc_config::Selection;
use quux_otelc_rust::policy::Plan;
use serde::Serialize;
use std::collections::HashSet;
use syn::{
    spanned::Spanned,
    visit::{self, Visit},
};
const MARKER: &str = "quux.otelc.generated";
#[derive(Debug, Serialize)]
pub struct Function {
    pub name: String,
    pub line: usize,
    pub selected: bool,
    pub unsupported: Option<&'static str>,
}
struct Parser<'a> {
    source: &'a str,
    scope: Vec<String>,
    functions: Vec<Function>,
    edits: Vec<(usize, String)>,
    selection: Selection,
    selected_source: bool,
    plan: &'a Plan,
    startup: bool,
    inspect: bool,
    guard: String,
    shutdown: String,
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
            name: name.clone(),
            line: sig.fn_token.span.start().line,
            selected,
            unsupported,
        });
        let mut code = String::new();
        if self.startup && root && sig.ident == "main" {
            // Executor attribute macros control async entry-point shutdown;
            // their expansion is outside this source adapter's current contract.
            if sig.asyncness.is_some() {
                self.error = Some(anyhow::anyhow!(
                    "Rust async main requires executor entry-point qualification"
                ));
                return;
            }
            if let Some(reason) = unsupported {
                self.error = Some(anyhow::anyhow!("Rust entry point: {reason}"));
                return;
            }
            code.push_str(&format!(
                "let {}=::quux_otelc_rust::launch();",
                self.shutdown
            ));
        }
        if selected {
            if let Some(reason) = unsupported {
                if !self.inspect {
                    self.error = Some(anyhow::anyhow!("{name}: {reason}"));
                }
                return;
            }
            if self.plan.annotations.inject_generated {
                code.push_str("/*otelc.instrument*/");
            }
            if sig.asyncness.is_some() {
                // Keep the original async signature and parameter ownership.
                // Only the body is nested; the native await polls it directly,
                // retaining Pending, wakeups, Send bounds and panic payloads.
                code.push_str(&format!(
                    "::quux_otelc_rust::observe_future({name:?},async move{{"
                ));
                // Rust async-fn parameters drop in reverse declaration order.
                // Shadow all named parameters in that order inside the body;
                // otherwise async-block capture-field order changes cleanup.
                // The receiver is first and drops after these body bindings.
                for input in &sig.inputs {
                    if let syn::FnArg::Typed(input) = input {
                        if let syn::Pat::Ident(binding) = &*input.pat {
                            let name = &binding.ident;
                            if binding.mutability.is_some() {
                                code.push_str(&format!("let _=&mut {name};let mut {name}={name};"));
                            } else {
                                code.push_str(&format!("let {name}={name};"));
                            }
                        }
                    }
                }
                if let syn::ReturnType::Type(_, output) = &sig.output {
                    let mut opaque = Opaque(false);
                    opaque.visit_type(output);
                    if !opaque.0 {
                        let mut coercions = Coercions {
                            source: self.source,
                            output: output.to_token_stream().to_string(),
                            edits: &mut self.edits,
                        };
                        coercions.visit_block(body);
                        if let Some(syn::Stmt::Expr(expression, None)) = body.stmts.last() {
                            if !matches!(expression, syn::Expr::Return(_)) {
                                coercions.expression(expression);
                            }
                        }
                    }
                }
                self.edits.push((
                    offset(self.source, body.brace_token.span.close()),
                    "}).await".into(),
                ));
            } else {
                code.push_str(&format!(
                    "let {}=::quux_otelc_rust::enter({name:?});",
                    self.guard
                ));
            }
        }
        if !code.is_empty() {
            self.edits.push((
                offset(self.source, body.brace_token.span.open()) + 1,
                format!("/*{MARKER}*/{code}"),
            ));
        }
    }
}
struct Opaque(bool);
impl<'ast> Visit<'ast> for Opaque {
    fn visit_type_impl_trait(&mut self, _: &'ast syn::TypeImplTrait) {
        self.0 = true;
    }
}
struct Coercions<'a> {
    source: &'a str,
    output: String,
    edits: &'a mut Vec<(usize, String)>,
}
impl Coercions<'_> {
    fn expression(&mut self, expression: &syn::Expr) {
        self.edits.push((
            offset(self.source, expression.span()),
            format!("::std::convert::identity::<{}>(", self.output),
        ));
        self.edits.push((
            position_offset(self.source, expression.span().end()),
            ")".into(),
        ));
    }
}
impl<'ast> Visit<'ast> for Coercions<'_> {
    fn visit_item(&mut self, _: &'ast syn::Item) {}
    fn visit_expr_closure(&mut self, _: &'ast syn::ExprClosure) {}
    fn visit_expr_async(&mut self, _: &'ast syn::ExprAsync) {}
    fn visit_expr_return(&mut self, node: &'ast syn::ExprReturn) {
        if let Some(expression) = &node.expr {
            self.expression(expression);
            self.visit_expr(expression);
        }
    }
}
impl<'ast> Visit<'ast> for Parser<'_> {
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
    fn visit_macro(&mut self, node: &'ast syn::Macro) {
        if self.selected_source && node.path.is_ident("include") {
            self.error = Some(anyhow::anyhow!(
                "Rust include! source fragments require separate qualification"
            ));
        }
        visit::visit_macro(self, node);
    }
}
fn identifiers(tokens: TokenStream, used: &mut HashSet<String>) {
    for token in tokens {
        match token {
            TokenTree::Ident(value) => {
                used.insert(value.to_string().trim_start_matches("r#").into());
            }
            TokenTree::Group(group) => identifiers(group.stream(), used),
            _ => {}
        }
    }
}
pub fn transform(
    source: &str,
    identity: &str,
    plan: &Plan,
    selected_source: bool,
    startup: bool,
    inspect: bool,
) -> Result<(String, Vec<Function>)> {
    if source.contains(&format!("/*{MARKER}*/")) {
        bail!("Rust input is already generated");
    }
    let parsed = syn::parse_file(source).context("parse original Rust source")?;
    let mut used = HashSet::new();
    identifiers(
        source
            .parse()
            .map_err(|error| anyhow::anyhow!("parse Rust tokens: {error}"))?,
        &mut used,
    );
    let unique = |prefix: &str| {
        let mut name = prefix.to_owned();
        while used.contains(&name) {
            name.push('_');
        }
        name
    };
    let mut parser = Parser {
        source,
        scope: vec![identity.into()],
        functions: vec![],
        edits: vec![],
        selection: Selection::new(&plan.functions.include, &plan.functions.exclude, false)?,
        selected_source,
        plan,
        startup,
        inspect,
        guard: unique("__quux_otelc_guard"),
        shutdown: unique("__quux_otelc_shutdown"),
        error: None,
    };
    parser.visit_file(&parsed);
    if let Some(error) = parser.error {
        return Err(error);
    }
    let mut generated = source.to_owned();
    parser
        .edits
        .sort_by_key(|(position, _)| std::cmp::Reverse(*position));
    if !inspect {
        for (position, code) in parser.edits {
            generated.insert_str(position, &code);
        }
    }
    Ok((generated, parser.functions))
}
