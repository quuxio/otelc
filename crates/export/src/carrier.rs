//! Bounded W3C carrier values, independent of local span-tree ownership.
use opentelemetry::trace::{SpanContext, SpanId, TraceFlags, TraceId};
use std::collections::HashSet;

const MAX_PARENT_BYTES: usize = 512;
const MAX_STATE_INPUT_BYTES: usize = 32768;
const MAX_STATE_OUTPUT_BYTES: usize = 512;
const KNOWN_FLAGS: u8 = 3; // Sampled plus the SDK random-trace-ID extension.

#[derive(Debug, PartialEq, Eq)]
pub struct Headers {
    pub traceparent: String,
    pub tracestate: Option<String>,
}

/// Validated wire identity. State remains independent of SDK-specific validators.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Context {
    trace: TraceId,
    parent: SpanId,
    flags: TraceFlags,
    state: Option<String>,
}

impl Context {
    pub fn trace_id(&self) -> TraceId {
        self.trace
    }
    pub fn span_id(&self) -> SpanId {
        self.parent
    }
    pub fn trace_flags(&self) -> TraceFlags {
        self.flags
    }

    pub fn from_sdk(context: &SpanContext) -> Option<Self> {
        context.is_valid().then(|| Self {
            trace: context.trace_id(),
            parent: context.span_id(),
            flags: TraceFlags::new(context.trace_flags().to_u8() & KNOWN_FLAGS),
            state: state(&[&context.trace_state().header()]),
        })
    }
}

fn key_char(byte: u8) -> bool {
    byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_-*/".contains(&byte)
}

fn valid_key(key: &str) -> bool {
    if let Some((tenant, system)) = key.split_once('@') {
        return !tenant.is_empty()
            && tenant.len() <= 241
            && tenant.as_bytes()[0].is_ascii_alphanumeric()
            && tenant.bytes().all(key_char)
            && !system.is_empty()
            && system.len() <= 14
            && system.as_bytes()[0].is_ascii_lowercase()
            && system.bytes().all(key_char);
    }
    !key.is_empty()
        && key.len() <= 256
        && key.as_bytes()[0].is_ascii_lowercase()
        && key.bytes().all(key_char)
}

fn state_size(pairs: &[(&str, &str)]) -> usize {
    pairs
        .iter()
        .map(|(k, v)| k.len() + v.len() + 1)
        .sum::<usize>()
        + pairs.len().saturating_sub(1)
}

fn parse_pairs(value: &str) -> Option<Vec<(&str, &str)>> {
    let members: Vec<_> = value.split(',').take(33).collect();
    if members.len() > 32 {
        return None;
    }
    let mut pairs = Vec::new();
    let mut keys = HashSet::new();
    for member in members {
        let member = member.trim_matches([' ', '\t']);
        if member.is_empty() {
            continue;
        }
        let (key, value) = member.split_once('=')?;
        if !valid_key(key)
            || value.is_empty()
            || value.len() > 256
            || !value
                .bytes()
                .all(|b| (0x20..=0x7e).contains(&b) && b != b',' && b != b'=')
            || !keys.insert(key)
        {
            return None;
        }
        pairs.push((key, value));
    }
    Some(pairs)
}

fn state(fields: &[&str]) -> Option<String> {
    if fields.len() > 32
        || fields
            .iter()
            .try_fold(0usize, |size, field| size.checked_add(field.len()))
            .is_none_or(|size| {
                size.saturating_add(fields.len().saturating_sub(1)) > MAX_STATE_INPUT_BYTES
            })
    {
        return None;
    }
    let joined = fields.join(",");
    let mut pairs = parse_pairs(&joined)?;
    // Prefer removing large, less-recent entries, then remove from the right.
    for index in (0..pairs.len()).rev() {
        if state_size(&pairs) <= MAX_STATE_OUTPUT_BYTES {
            break;
        }
        if pairs[index].0.len() + pairs[index].1.len() + 1 > 128 {
            pairs.remove(index);
        }
    }
    while state_size(&pairs) > MAX_STATE_OUTPUT_BYTES {
        pairs.pop();
    }
    (!pairs.is_empty()).then(|| {
        pairs
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .collect::<Vec<_>>()
            .join(",")
    })
}

/// Callers supply all occurrences of each case-insensitive HTTP field name.
/// Invalid state is discarded independently; invalid/duplicate parents return None.
pub fn extract(parents: &[&str], states: &[&str]) -> Option<Context> {
    if parents.len() != 1 || parents[0].len() > MAX_PARENT_BYTES {
        return None;
    }
    let value = parents[0].trim_matches([' ', '\t']);
    let bytes = value.as_bytes();
    if bytes.len() < 55
        || bytes
            .iter()
            .any(|b| (*b < 0x20 && *b != b'\t') || *b == 0x7f)
        || [2, 35, 52].iter().any(|index| bytes[*index] != b'-')
        || !bytes[..2]
            .iter()
            .chain(&bytes[3..35])
            .chain(&bytes[36..52])
            .chain(&bytes[53..55])
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    {
        return None;
    }
    let version = u8::from_str_radix(&value[..2], 16).ok()?;
    if version == 255 || version == 0 && bytes.len() != 55 || bytes.len() > 55 && bytes[55] != b'-'
    {
        return None;
    }
    let trace = TraceId::from_hex(&value[3..35]).ok()?;
    let parent = SpanId::from_hex(&value[36..52]).ok()?;
    if trace == TraceId::INVALID || parent == SpanId::INVALID {
        return None;
    }
    let flags = u8::from_str_radix(&value[53..55], 16).ok()? & KNOWN_FLAGS;
    Some(Context {
        trace,
        parent,
        flags: TraceFlags::new(flags),
        state: state(states),
    })
}

/// Emit version 00 with known flags and bounded ordered state; omit invalid IDs.
pub fn inject(context: &Context) -> Headers {
    Headers {
        traceparent: format!(
            "00-{}-{}-{:02x}",
            context.trace_id(),
            context.span_id(),
            context.trace_flags().to_u8() & KNOWN_FLAGS
        ),
        tracestate: context.state.clone(),
    }
}

#[cfg(test)]
#[path = "tests/carrier.rs"]
mod tests;
