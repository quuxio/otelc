//! Bounded whole-tree buffering shared by native and Rust adapters.
use opentelemetry::{
    trace::{SpanContext, SpanId, SpanKind, Status, TraceFlags, TraceId, TraceState},
    InstrumentationScope, KeyValue,
};
use opentelemetry_sdk::trace::{
    IdGenerator, RandomIdGenerator, Sampler, SamplingDecision, ShouldSample, SpanData,
};
use quux_otelc_config::Traces;
use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    sync::Arc,
    time::{Instant, SystemTime},
};

#[derive(Clone, Copy, Debug)]
pub struct Context {
    pub owner: u64,
    root: u64,
    trace: TraceId,
    span: SpanId,
    pub sampled: bool,
}
struct Node {
    id: SpanId,
    parent: SpanId,
    name: Arc<str>,
    started: Instant,
    ended: Option<Instant>,
    unwind: bool,
    cancelled: bool,
}
struct Tree {
    trace: TraceId,
    origin: Instant,
    epoch: SystemTime,
    nodes: Vec<Node>,
    positions: HashMap<SpanId, usize>,
    active: usize,
    closed: bool,
    invalid: bool,
}
#[derive(Default)]
pub struct Store {
    roots: HashMap<u64, Tree>,
    ready: VecDeque<Tree>,
    next: u64,
    retained: usize,
    pub completed: u64,
    pub sampled_out: u64,
    pub losses: BTreeMap<&'static str, u64>,
}
fn valid_id<T: PartialEq>(mut generate: impl FnMut() -> T, invalid: T) -> Option<T> {
    (0..3).map(|_| generate()).find(|value| *value != invalid)
}
impl Store {
    /// Includes active and queued nodes; excludes the single popped export tree.
    pub fn retained(&self) -> usize {
        self.retained
    }
    fn lose(&mut self, reason: &'static str) {
        let value = self.losses.entry(reason).or_default();
        *value = value.saturating_add(1);
    }
    pub fn reject(&mut self, owner: u64, parent: Option<Context>, reason: &'static str) -> Context {
        if let Some(parent) = parent {
            if let Some(tree) = self.roots.get_mut(&parent.root) {
                if !tree.invalid {
                    tree.invalid = true;
                    self.retained -= tree.nodes.len();
                    tree.nodes = Vec::new();
                    tree.positions = HashMap::new();
                    self.lose(reason);
                }
            }
            Context {
                sampled: false,
                ..parent
            }
        } else {
            self.lose(reason);
            Context {
                owner,
                root: 0,
                trace: TraceId::INVALID,
                span: SpanId::INVALID,
                sampled: false,
            }
        }
    }
    pub fn enter(
        &mut self,
        owner: u64,
        parent: Option<Context>,
        name: Arc<str>,
        started: Instant,
        policy: &Traces,
    ) -> Context {
        self.enter_with_epoch(owner, parent, name, started, None, policy)
    }
    /// The native queue supplies its producer epoch, independent of drain delay.
    pub fn enter_at_epoch(
        &mut self,
        owner: u64,
        parent: Option<Context>,
        name: Arc<str>,
        started: Instant,
        epoch: SystemTime,
        policy: &Traces,
    ) -> Context {
        self.enter_with_epoch(owner, parent, name, started, Some(epoch), policy)
    }
    fn enter_with_epoch(
        &mut self,
        owner: u64,
        parent: Option<Context>,
        name: Arc<str>,
        started: Instant,
        epoch: Option<SystemTime>,
        policy: &Traces,
    ) -> Context {
        if let Some(parent) = parent {
            if !parent.sampled || !self.roots.contains_key(&parent.root) {
                return Context {
                    sampled: false,
                    ..parent
                };
            }
            let tree = &self.roots[&parent.root];
            if tree.invalid {
                return Context {
                    sampled: false,
                    ..parent
                };
            }
            if tree.nodes.len() >= policy.max_spans_per_trace || self.retained >= 1_048_576 {
                return self.reject(owner, Some(parent), "span_capacity");
            }
            let ids = RandomIdGenerator::default();
            let Some(id) = valid_id(|| ids.new_span_id(), SpanId::INVALID) else {
                return self.reject(owner, Some(parent), "invalid");
            };
            if tree.positions.contains_key(&id) {
                return self.reject(owner, Some(parent), "invalid");
            }
            let tree = self
                .roots
                .get_mut(&parent.root)
                .expect("root checked under mutex");
            tree.active += 1;
            tree.positions.insert(id, tree.nodes.len());
            tree.nodes.push(Node {
                id,
                parent: parent.span,
                name,
                started,
                ended: None,
                unwind: false,
                cancelled: false,
            });
            self.retained += 1;
            return Context { span: id, ..parent };
        }
        let epoch = epoch.unwrap_or_else(SystemTime::now);
        let ids = RandomIdGenerator::default();
        let Some(trace) = valid_id(|| ids.new_trace_id(), TraceId::INVALID) else {
            return self.reject(owner, None, "invalid");
        };
        let sampled = Sampler::TraceIdRatioBased(policy.root_sample_ratio)
            .should_sample(None, trace, &name, &SpanKind::Internal, &[], &[])
            .decision
            == SamplingDecision::RecordAndSample;
        if !sampled {
            self.sampled_out = self.sampled_out.saturating_add(1);
            return Context {
                owner,
                root: 0,
                trace,
                span: SpanId::INVALID,
                sampled: false,
            };
        }
        if self.roots.len() >= policy.max_active_traces || self.retained >= 1_048_576 {
            return self.reject(owner, None, "trace_capacity");
        }
        let Some(root) = self.next.checked_add(1) else {
            return self.reject(owner, None, "invalid");
        };
        self.next = root;
        let Some(span) = valid_id(|| ids.new_span_id(), SpanId::INVALID) else {
            return self.reject(owner, None, "invalid");
        };
        self.roots.insert(
            root,
            Tree {
                trace,
                origin: started,
                epoch,
                active: 1,
                closed: false,
                invalid: false,
                nodes: vec![Node {
                    id: span,
                    parent: SpanId::INVALID,
                    name,
                    started,
                    ended: None,
                    unwind: false,
                    cancelled: false,
                }],
                positions: HashMap::from([(span, 0)]),
            },
        );
        self.retained += 1;
        Context {
            owner,
            root,
            trace,
            span,
            sampled: true,
        }
    }
    pub fn finish(
        &mut self,
        context: Context,
        ended: Instant,
        unwind: bool,
        cancelled: bool,
        queue_capacity: usize,
    ) {
        if !context.sampled {
            return;
        }
        let Some(tree) = self.roots.get_mut(&context.root) else {
            return;
        };
        debug_assert_eq!(tree.trace, context.trace);
        // Invalid trees release their payload immediately but keep active bookkeeping.
        if !tree.invalid {
            let Some(index) = tree.positions.get(&context.span).copied() else {
                return;
            };
            let node = &mut tree.nodes[index];
            if node.ended.is_some() {
                return;
            }
            node.ended = Some(ended.max(node.started));
            node.unwind = unwind;
            node.cancelled = cancelled;
        }
        tree.active -= 1;
        if context.span == tree.nodes.first().map_or(SpanId::INVALID, |node| node.id)
            || tree.invalid && tree.active == 0
        {
            tree.closed = true;
        }
        if !tree.closed || tree.active != 0 {
            return;
        }
        let tree = self
            .roots
            .remove(&context.root)
            .expect("completed root present");
        if tree.invalid {
            return;
        }
        if self.ready.len() >= queue_capacity {
            self.retained -= tree.nodes.len();
            self.lose("queue_capacity");
        } else {
            self.completed = self.completed.saturating_add(1);
            self.ready.push_back(tree);
        }
    }
    pub fn shutdown(&mut self) {
        let roots = std::mem::take(&mut self.roots);
        for tree in roots.into_values() {
            self.retained -= tree.nodes.len();
            if !tree.invalid {
                self.lose("incomplete");
            }
        }
    }
    /// Drop queued whole trees without allocating SDK or protobuf payloads.
    pub fn discard_ready(&mut self, reason: &'static str) {
        while let Some(tree) = self.ready.pop_front() {
            self.retained -= tree.nodes.len();
            self.lose(reason);
        }
    }
    pub fn pop(&mut self) -> Option<Vec<SpanData>> {
        let tree = self.ready.pop_front()?;
        self.retained -= tree.nodes.len();
        // Each function name is capped at 1 KiB. Reject oversized trees before
        // duplicating names into SDK/protobuf structures (16 MiB wire budget).
        if tree
            .nodes
            .iter()
            .map(|node| node.name.len() * 2 + 256)
            .sum::<usize>()
            > 16 * 1024 * 1024
        {
            self.lose("batch_bytes");
            return Some(Vec::new());
        }
        let scope = InstrumentationScope::builder("quux.otelc")
            .with_version(env!("CARGO_PKG_VERSION"))
            .build();
        Some(
            tree.nodes
                .into_iter()
                .map(|node| {
                    let start_time =
                        tree.epoch + node.started.saturating_duration_since(tree.origin);
                    let end_time = tree.epoch
                        + node
                            .ended
                            .expect("only completed trees exported")
                            .saturating_duration_since(tree.origin);
                    let mut attributes =
                        vec![KeyValue::new("code.function.name", node.name.to_string())];
                    if node.cancelled {
                        attributes.push(KeyValue::new("otelc.cancelled", true));
                    }
                    SpanData {
                        span_context: SpanContext::new(
                            tree.trace,
                            node.id,
                            TraceFlags::SAMPLED,
                            false,
                            TraceState::default(),
                        ),
                        parent_span_id: node.parent,
                        parent_span_is_remote: false,
                        span_kind: SpanKind::Internal,
                        name: node.name.to_string().into(),
                        start_time,
                        end_time,
                        attributes,
                        dropped_attributes_count: 0,
                        events: Default::default(),
                        links: Default::default(),
                        status: if node.unwind {
                            Status::error("escaping unwind")
                        } else if node.cancelled {
                            Status::error("cancelled")
                        } else {
                            Status::Unset
                        },
                        instrumentation_scope: scope.clone(),
                    }
                })
                .collect(),
        )
    }
    pub fn report(&self) -> serde_json::Value {
        serde_json::json!({"completed_trees":self.completed,"sampled_out_roots":self.sampled_out,"active_trees":self.roots.len(),"queued_trees":self.ready.len(),"losses":self.losses})
    }
}

#[cfg(test)]
#[path = "tests/trace_store.rs"]
mod tests;
