//! Bounded whole-tree buffering. Application values never enter this store.
use opentelemetry::{
    trace::{SpanContext, SpanId, SpanKind, Status, TraceFlags, TraceId, TraceState},
    InstrumentationScope, KeyValue,
};
use opentelemetry_sdk::trace::{
    IdGenerator, RandomIdGenerator, Sampler, SamplingDecision, ShouldSample, SpanData,
};
use quux_otelc_config::Traces;
use std::{
    cell::Cell,
    collections::{BTreeMap, HashMap, VecDeque},
    marker::PhantomData,
    rc::Rc,
    sync::Arc,
    time::{Instant, SystemTime},
};

#[derive(Clone, Copy, Debug)]
pub(crate) struct Context {
    pub owner: u64,
    root: u64,
    trace: TraceId,
    span: SpanId,
    pub sampled: bool,
}
thread_local! {
    static CURRENT: Cell<Option<Context>> = const { Cell::new(None) };
}
pub(crate) fn current(owner: u64) -> Option<Context> {
    CURRENT.with(|cell| cell.get().filter(|context| context.owner == owner))
}
/// Kept on the synchronous stack or inside a single poll, never across await.
pub(crate) struct Scope {
    previous: Option<Context>,
    _thread: PhantomData<Rc<()>>,
}
impl Scope {
    pub fn attach(context: Option<Context>) -> Self {
        Self {
            previous: CURRENT.with(|cell| cell.replace(context)),
            _thread: PhantomData,
        }
    }
}
impl Drop for Scope {
    fn drop(&mut self) {
        CURRENT.with(|cell| cell.set(self.previous));
    }
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
pub(crate) struct Store {
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
        let epoch = SystemTime::now();
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

pin_project_lite::pin_project! {
    pub(crate) struct InContext<F> {
        #[pin]
        pub future: Option<F>,
        pub context: Option<Context>,
    }
    impl<F> PinnedDrop for InContext<F> {
        fn drop(this: Pin<&mut Self>) {
            let mut this = this.project();
            let _scope = Scope::attach(*this.context);
            this.future.set(None);
        }
    }
}
impl<F: std::future::Future> std::future::Future for InContext<F> {
    type Output = F::Output;
    fn poll(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        let this = self.project();
        let _scope = Scope::attach(*this.context);
        this.future
            .as_pin_mut()
            .expect("future present until drop")
            .poll(cx)
    }
}

#[cfg(test)]
#[path = "tests/trace_store.rs"]
mod tests;
