package io.quux.otelc.witness;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import java.io.IOException;
import java.lang.StackWalker.Option;
import java.nio.file.Path;
import java.time.Duration;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.BiConsumer;
import java.util.function.BiFunction;

/** Synchronous diagnostic actions; no application values or throwable graphs are retained. */
public final class WitnessRuntime {
  static final String SCOPE = "otelc.witness.scope_id";
  static final String ROLE = "otelc.witness.role";
  private static final String ENVELOPE = "envelope";
  private static final String RENDEZVOUS = "rendezvous";
  private static final String WRAPPER = "wrapper";
  private static final String TARGET = "target";
  private static final String ENTERED = "entered";
  private static final String READY = "ready";
  private static final String ARMED = "armed";
  private static final String CONSUMED = "consumed";
  private static final String INCOMPLETE = "incomplete";
  private static final String EXPIRED = "expired";
  private static final String CANCELLED = "cancelled";
  private static final int MAX_SCOPES = 64;
  private static final long LIFE = Duration.ofSeconds(30).toNanos();
  private static final java.util.concurrent.atomic.AtomicReference<WitnessRuntime> INSTANCE =
      new java.util.concurrent.atomic.AtomicReference<>();
  private static final StackWalker WALKER = StackWalker.getInstance(Set.of(Option.RETAIN_CLASS_REFERENCE,
      Option.SHOW_REFLECT_FRAMES, Option.SHOW_HIDDEN_FRAMES));
  private final WitnessPlan plan;
  private final WitnessClassCapture capture;
  private final java.util.function.LongSupplier clock;
  private final String runtime = fresh();
  private final long pid = ProcessHandle.current().pid();
  private final Map<String, WitnessBindings.Binding> probes = new HashMap<>();
  private final Set<String> guarded = new HashSet<>();
  private final Map<String, WitnessScope> history = new LinkedHashMap<>();
  private final ThreadLocal<Entry> current = new ThreadLocal<>();
  private Map<String, WitnessClassCapture.Loaded> loaded = Map.of();
  private OpenTelemetrySdk sdk;
  private Class<?> helper;
  private WitnessScope active;
  private WitnessScope latest;
  private String failure;
  private long sequence;
  private long rejected;

  private static final class Entry {
    final Entry parent;
    final String role;
    final WitnessScope scope;
    Context context;
    io.opentelemetry.context.Scope attached;
    Span span;
    boolean handleObserved;
    boolean routeVerified;
    Entry(Entry parent, String role, WitnessScope scope) {
      this.parent = parent; this.role = role; this.scope = scope;
    }
  }

  public WitnessRuntime(WitnessPlan plan, WitnessClassCapture capture) {
    this(plan, capture, System::nanoTime);
  }

  WitnessRuntime(WitnessPlan plan, WitnessClassCapture capture, java.util.function.LongSupplier clock) {
    this.plan = plan;
    this.capture = capture;
    this.clock = clock;
    plan.bindings().values().forEach(binding -> probes.put(binding.probeId(), binding));
  }

  public static WitnessRuntime get() { return INSTANCE.get(); }

  public static void configure(String file) {
    if (file == null) return;
    try {
      WitnessPlan plan = WitnessPlan.load(Path.of(file));
      var instrumentation = io.opentelemetry.javaagent.bootstrap.InstrumentationHolder.getInstrumentation();
      INSTANCE.set(new WitnessRuntime(plan, WitnessClassCapture.install(instrumentation, plan.bindings(), plan.origins(), plan.route())));
    } catch (IOException error) {
      throw new IllegalStateException("cannot read private witness plan", error);
    }
  }

  public List<WitnessBindings.Binding> bindings(String binaryName) {
    return plan.bindings().values().stream()
        .filter(binding -> binding.declaration().owner().replace('/', '.').equals(binaryName)).toList();
  }

  public Set<String> owners() {
    return Set.copyOf(plan.origins().keySet().stream().map(owner -> owner.replace('/', '.')).toList());
  }

  public synchronized void guarded(WitnessBindings.Binding binding) { guarded.add(binding.probeId()); }

  public HandleBindings.Route route() { return plan.route(); }

  public void attach(OpenTelemetrySdk sdk) throws IOException {
    this.sdk = sdk;
    try {
      for (String owner : owners()) Class.forName(owner, false, ClassLoader.getSystemClassLoader());
      capture.verifyBindings();
      loaded = capture.verifyLoaded();
      if (!guarded.equals(probes.keySet())) throw new IllegalStateException("role guards did not match");
      helper = Class.forName("io.quux.otelc.witness.probe.Observer", false, ClassLoader.getSystemClassLoader());
      helper.getMethod("install", BiFunction.class, BiConsumer.class).invoke(null,
          (BiFunction<Class<?>, String, Object>) this::enter, (BiConsumer<Object, Throwable>) this::exit);
      if (plan.route() != null) helper.getMethod("installHandle", java.util.function.Consumer.class,
          java.util.function.Function.class).invoke(null,
              (java.util.function.Consumer<java.lang.invoke.MethodHandle>) this::observeHandle,
              (java.util.function.Function<Object[], Object>) values -> enter((Class<?>) values[0],
                  (String) values[1], (Class<?>) values[2]));
    } catch (ReflectiveOperationException | RuntimeException | LinkageError unavailable) {
      failure = "probe_binding_failed";
    }
    WitnessServer.start(plan.socket(), this);
  }

  private boolean validCapture() {
    try { capture.verifyLoaded(); return true; }
    catch (IllegalStateException unavailable) {
      synchronized (this) {
        failure = "class_identity_invalid";
        if (active != null) active.fail(failure);
      }
      return false;
    }
  }

  private boolean frame(StackWalker.StackFrame frame, WitnessBindings.Declaration declaration) {
    var type = loaded.get(declaration.owner());
    return type != null && frame.getDeclaringClass() == type.type()
        && frame.getMethodName().equals(declaration.name()) && frame.getDescriptor().equals(declaration.descriptor());
  }

  private boolean actualCall(Class<?> owner, WitnessBindings.Binding binding, boolean recursive) {
    var type = loaded.get(binding.declaration().owner());
    if (type == null || type.type() != owner) return false;
    return WALKER.walk(stream -> {
      var frames = stream.toList();
      for (int index = 0; index + 1 < frames.size(); index++) {
        if (frames.get(index).getDeclaringClass() != helper || !frames.get(index).getMethodName().equals("enter")) continue;
        return matchesCall(frames, index, binding, recursive);
      }
      return false;
    });
  }

  private boolean matchesCall(List<StackWalker.StackFrame> frames, int index,
                              WitnessBindings.Binding binding, boolean recursive) {
    if (!frame(frames.get(index + 1), binding.declaration())) return false;
    if (plan.route() != null && binding.role().equals(TARGET)) {
      if (recursive) return index + 2 < frames.size() && frame(frames.get(index + 2), binding.declaration());
      int caller = index + 2;
      while (caller < frames.size() && HandleIdentity.machinery(frames.get(caller))) caller++;
      return caller < frames.size() && frame(frames.get(caller), plan.bindings().get(WRAPPER).declaration());
    }
    String caller = switch (binding.role()) {
      case RENDEZVOUS, WRAPPER -> ENVELOPE;
      case TARGET -> recursive ? null : WRAPPER;
      default -> null;
    };
    return caller == null || index + 2 < frames.size()
        && frame(frames.get(index + 2), plan.bindings().get(caller).declaration());
  }

  private Object enter(Class<?> owner, String probe) {
    return enter(owner, probe, null);
  }

  private Object enter(Class<?> owner, String probe, Class<?> receiver) {
    Entry parent = current.get();
    WitnessBindings.Binding binding = probes.get(probe);
    boolean recursive = parent != null && parent.scope != null && parent.scope.targetEntries > 0 && !parent.scope.targetExited;
    if (binding == null || !actualCall(owner, binding, recursive)) {
      synchronized (this) { rejected++; }
      return rejected(parent, binding == null ? "rejected" : binding.role());
    }
    if (plan.route() != null && binding.role().equals(TARGET)) {
      boolean exactReceiver = plan.route().referenceKind() == java.lang.invoke.MethodHandleInfo.REF_invokeStatic
          ? receiver == null : receiver == owner;
      if (!exactReceiver || !recursive && (parent == null || !parent.routeVerified)) {
        synchronized (this) {
          rejected++;
          if (parent != null && parent.scope != null) parent.scope.fail("unverified_handle_route");
        }
        return rejected(parent, binding.role());
      }
    }
    if (Thread.currentThread().isVirtual()) {
      synchronized (this) {
        rejected++;
        if (binding.role().equals(ENVELOPE)) failure = "unsupported_virtual_thread";
      }
      return rejected(parent, binding.role());
    }
    WitnessScope scope = selectScope(binding, parent, recursive);
    Entry entry = new Entry(parent, binding.role(), scope);
    current.set(entry);
    if (scope == null) {
      if (parent != null && parent.scope != null) entry.attached = Context.root().makeCurrent();
      return entry;
    }
    if ((binding.role().equals(WRAPPER) || binding.role().equals(TARGET)) && scope.id != null && scope.failure == null) {
      try { startSpan(entry, binding, recursive); }
      catch (RuntimeException | LinkageError unavailable) {
        synchronized (this) { scope.fail("role_span_failed"); }
      }
    }
    return entry;
  }

  private void observeHandle(java.lang.invoke.MethodHandle operand) {
    Entry entry = current.get();
    if (entry == null || !entry.role.equals(WRAPPER) || entry.scope == null) return;
    boolean actual = WALKER.walk(stream -> {
      var frames = stream.toList();
      for (int index = 0; index + 1 < frames.size(); index++) {
        if (frames.get(index).getDeclaringClass() == helper && frames.get(index).getMethodName().equals("handle")) {
          return frame(frames.get(index + 1), plan.bindings().get(WRAPPER).declaration());
        }
      }
      return false;
    });
    synchronized (this) {
      WitnessScope scope = entry.scope;
      if (!actual || active != scope || !scope.phase.equals(CONSUMED)
          || scope.thread.get() != Thread.currentThread() || scope.failure != null) return;
      if (entry.handleObserved) { scope.fail("duplicate_handle_operand"); entry.routeVerified = false; return; }
      entry.handleObserved = true;
      var target = plan.bindings().get(TARGET).declaration();
      var loadedTarget = loaded.get(target.owner());
      if (loadedTarget == null || !HandleIdentity.matches(operand, loadedTarget.type(), plan.route(), target)) {
        scope.fail("handle_identity_mismatch");
        return;
      }
      entry.routeVerified = true;
      scope.routeVerified = true;
    }
  }

  private synchronized WitnessScope selectScope(WitnessBindings.Binding binding, Entry parent, boolean recursive) {
    expire();
    WitnessScope scope = parent == null ? null : parent.scope;
    if (binding.role().equals(ENVELOPE)) scope = begin();
    if (scope != null && !binding.role().equals(ENVELOPE)) {
      if (!ownedParent(binding.role(), parent, recursive)) { rejected++; return null; }
      advance(scope, binding.role(), parent, recursive);
    }
    return scope;
  }

  private Entry rejected(Entry parent, String role) {
    Entry entry = new Entry(parent, role, null);
    current.set(entry);
    if (parent != null && parent.scope != null) entry.attached = Context.root().makeCurrent();
    return entry;
  }

  private boolean ownedParent(String role, Entry parent, boolean recursive) {
    return switch (role) {
      case RENDEZVOUS, WRAPPER -> parent.role.equals(ENVELOPE);
      case TARGET -> recursive || parent.role.equals(WRAPPER);
      default -> false;
    };
  }

  WitnessScope begin() {
    if (failure != null || active != null) { rejected++; return null; }
    if (history.size() >= MAX_SCOPES) { failure = "scope_capacity"; rejected++; return null; }
    WitnessScope scope = new WitnessScope(fresh(), ++sequence, Thread.currentThread(), clock.getAsLong() + LIFE, plan.route() != null);
    active = scope; latest = scope; history.put(scope.cookie, scope);
    return scope;
  }

  private void advance(WitnessScope scope, String role, Entry parent, boolean recursive) {
    if (scope.thread.get() != Thread.currentThread()) { scope.fail("wrong_thread"); return; }
    switch (role) {
      case RENDEZVOUS -> {
        if (!parent.role.equals(ENVELOPE) || !scope.phase.equals(ENTERED)) scope.fail("invalid_rendezvous");
        else scope.phase = READY;
      }
      case WRAPPER -> {
        if (!parent.role.equals(ENVELOPE) || !scope.phase.equals(ARMED)) scope.fail("unarmed_wrapper");
        else scope.phase = CONSUMED;
      }
      case TARGET -> {
        if (!recursive && (!parent.role.equals(WRAPPER) || !scope.phase.equals(CONSUMED))) scope.fail("invalid_target");
        else scope.targetEntries++;
      }
      default -> throw new IllegalArgumentException("invalid role");
    }
  }

  private void startSpan(Entry entry, WitnessBindings.Binding binding, boolean recursive) {
    WitnessScope scope = entry.scope;
    Context parent = binding.role().equals(WRAPPER) ? Context.root() : Context.current();
    // The start processor attaches identities once; duplicate writes inflate SDK loss accounting.
    var builder = sdk.getTracer("io.quux.otelc.witness", "1").spanBuilder("otelc.action." + binding.role())
        .setParent(parent).setAttribute(ROLE, recursive ? "recursive_target" : binding.role())
        .setAttribute("otelc.witness.probe_id", binding.probeId());
    entry.span = builder.startSpan();
    entry.context = parent.with(entry.span);
    entry.attached = entry.context.makeCurrent();
    if (!entry.span.isRecording()) synchronized (this) { scope.fail("unsampled_role_span"); }
  }

  private void exit(Object value, Throwable error) {
    if (!(value instanceof Entry entry) || current.get() != entry) return;
    try {
      if (entry.span != null) {
        if (error != null) entry.span.setStatus(StatusCode.ERROR).addEvent("exception",
            Attributes.of(AttributeKey.stringKey("exception.type"), error.getClass().getName()));
        entry.span.end();
      }
    } finally {
      entry.routeVerified = false;
      if (entry.attached != null) entry.attached.close();
      if (entry.parent == null) current.remove(); else current.set(entry.parent);
    }
    roleExited(entry);
  }

  private synchronized void roleExited(Entry entry) {
    WitnessScope scope = entry.scope;
    if (scope == null) return;
    switch (entry.role) {
      case TARGET -> scope.targetExited = scope.targetExited
          || entry.parent != null && entry.parent.role.equals(WRAPPER);
      case WRAPPER -> scope.rootExited = true;
      case ENVELOPE -> {
        scope.envelopeExited = true;
        if (scope.failure == null) scope.phase = "exited";
        else if (Set.of(ENTERED, READY, ARMED, CONSUMED).contains(scope.phase)) scope.phase = INCOMPLETE;
        if (active == scope) active = null;
      }
      default -> { /* Rendezvous exit only restores the envelope token. */ }
    }
  }

  private Attributes attributes(WitnessScope scope) {
    return Attributes.builder().put(SCOPE, scope.id).put("otelc.witness.runtime_id", runtime)
        .put("otelc.witness.context_cookie", scope.cookie).put("otelc.witness.case_id", scope.caseId)
        .put("otelc.witness.attempt_id", scope.attempt).put("otelc.witness.build_sha256", plan.build())
        .put("otelc.witness.selection_sha256", plan.selection()).put("otelc.witness.policy_sha256", plan.policy())
        .put("otelc.witness.tooling_sha256", plan.tooling()).build();
  }

  public synchronized void spanStarted(ReadWriteSpan span) {
    String id = span.getAttribute(AttributeKey.stringKey(SCOPE));
    WitnessScope scope = id == null ? currentScope() : find(id);
    if (scope == null || scope.id == null) return;
    String role = span.getAttribute(AttributeKey.stringKey(ROLE));
    String trace = span.getSpanContext().getTraceId();
    if (WRAPPER.equals(role)) {
      scope.trace = trace; scope.root = span.getSpanContext().getSpanId();
    } else if (!trace.equals(scope.trace)) return; // Lifetime roots with links are separate traces.
    if (scope.checkpoint) { scope.fail("late_span"); scope.phase = "invalidated"; }
    if (scope.spans.size() >= WitnessScope.MAX_SPANS) { scope.fail("span_capacity"); return; }
    span.setAllAttributes(attributes(scope));
    String spanId = span.getSpanContext().getSpanId();
    if (scope.spans.putIfAbsent(spanId, new WitnessScope.SpanState()) != null) scope.fail("duplicate_span");
    if (TARGET.equals(role)) scope.target = spanId;
  }

  private WitnessScope currentScope() {
    Entry entry = current.get();
    return entry == null ? null : entry.scope;
  }

  public synchronized void spanEnded(ReadableSpan span) {
    WitnessScope scope = find(span.getAttribute(AttributeKey.stringKey(SCOPE)));
    if (scope == null) return;
    var state = scope.spans.get(span.getSpanContext().getSpanId());
    if (state == null || state.ended) scope.fail("unmatched_span_end");
    else { state.ended = true; scope.checkpoint = false; }
  }

  record Export(String scope, String span) {}
  synchronized List<Export> exporting(java.util.Collection<io.opentelemetry.sdk.trace.data.SpanData> spans) {
    var result = new java.util.ArrayList<Export>();
    for (var span : spans) {
      WitnessScope scope = find(span.getAttributes().get(AttributeKey.stringKey(SCOPE)));
      if (scope != null) {
        var state = scope.spans.get(span.getSpanId());
        if (state == null) scope.fail("unmatched_span_export");
        else {
          state.inFlight++;
          state.attempts++;
          result.add(new Export(scope.id, span.getSpanId()));
        }
      }
    }
    return result;
  }

  synchronized void exported(List<Export> receipt, boolean success) {
    for (Export item : receipt) {
      WitnessScope scope = find(item.scope());
      var state = scope.spans.get(item.span());
      state.inFlight--;
      if (success) state.exported = true; else scope.exportFailures++;
    }
  }

  private WitnessScope find(String id) {
    if (id == null) return null;
    return history.values().stream().filter(scope -> id.equals(scope.id)).findFirst().orElse(null);
  }

  private void expire() {
    if (active != null && Set.of(ENTERED, READY, ARMED).contains(active.phase)
        && clock.getAsLong() - active.expires >= 0) { active.fail(EXPIRED); active.phase = EXPIRED; }
  }

  public String command(String text) {
    String[] words = text.split(" ", -1);
    if (words.length < 3 || !words[0].equals(Integer.toString(plan.version())) || !hex(words[1], 32)) return reply(null, "invalid_request", null);
    String request = words[1];
    if (!words[2].equals("status") && !validCapture()) return reply(request, "class_identity_invalid", latest);
    if (words[2].equals("end")) return end(words, request);
    synchronized (this) {
      expire();
      return switch (words[2]) {
        case "status" -> status(words, request);
        case "arm" -> arm(words, request);
        case "cancel" -> cancel(words, request);
        default -> reply(request, "unknown_operation", null);
      };
    }
  }

  private String status(String[] words, String request) {
    if (words.length == 3) return reply(request, failure, active == null ? latest : active);
    if (words.length == 5 && words[3].equals(runtime)) return reply(request, failure, find(words[4]));
    return reply(request, "invalid_request", null);
  }

  private String arm(String[] words, String request) {
    if (words.length != (plan.route() == null ? 14 : 15) || !words[3].equals(runtime) || active == null || !words[4].equals(active.cookie)
        || !words[5].equals(Long.toString(active.sequence)) || !hex(words[6], 64) || !hex(words[7], 64)
        || !words[8].equals(plan.build()) || !words[9].equals(plan.selection()) || !words[10].equals(plan.tooling())
        || !words[11].equals(plan.bindings().get(WRAPPER).probeId()) || !words[12].equals(plan.policy())
        || !words[13].equals(Long.toString(active.threadId))
        || plan.route() != null && !words[14].equals(plan.route().sha256())) {
      return reply(request, "arm_identity_mismatch", active);
    }
    if (active.failure != null || !active.phase.equals(READY)) return reply(request, "not_ready", active);
    active.id = fresh(); active.caseId = words[6]; active.attempt = words[7]; active.phase = ARMED;
    return reply(request, null, active);
  }

  private String cancel(String[] words, String request) {
    WitnessScope scope = words.length == 5 && words[3].equals(runtime) ? find(words[4]) : null;
    if (scope == null) return reply(request, "unknown_scope", null);
    if (scope.phase.equals("complete")) return reply(request, "scope_retired", scope);
    scope.fail(CANCELLED); scope.phase = CANCELLED;
    return reply(request, null, scope);
  }

  private String end(String[] words, String request) {
    WitnessScope scope;
    synchronized (this) {
      expire();
      scope = words.length == 5 && words[3].equals(runtime) ? find(words[4]) : null;
      if (scope == null) return reply(request, "unknown_scope", null);
      if (!scope.rootExited || !scope.envelopeExited) return reply(request, "root_active", scope);
      if (scope.targetEntries == 0) {
        scope.fail("target_not_entered");
        scope.phase = INCOMPLETE;
      }
      if (scope.spans.values().stream().anyMatch(span -> !span.ended)) {
        return reply(request, INCOMPLETE, scope);
      }
    }
    var flushed = sdk.getSdkTracerProvider().forceFlush();
    flushed.join(1500, TimeUnit.MILLISECONDS); // Never hold the scope monitor across SDK callbacks.
    synchronized (this) {
      if (flushed.isDone() && flushed.isSuccess()) scope.checkpoint = true; else scope.flushFailures++;
      if (scope.canComplete()) scope.phase = "complete";
      return reply(request, scope.complete() ? null : INCOMPLETE, scope);
    }
  }

  private synchronized String reply(String request, String error, WitnessScope scope) {
    return "{\"schema_version\":" + plan.version() + ",\"profile\":\"" + plan.profile() + "\",\"runtime_id\":\"" + runtime
        + "\",\"pid\":" + pid + ",\"request_id\":" + WitnessScope.value(request) + ",\"error\":" + WitnessScope.value(error)
        + ",\"build_sha256\":\"" + plan.build() + "\",\"selection_sha256\":\"" + plan.selection()
        + "\",\"policy_sha256\":\"" + plan.policy() + "\",\"tooling_sha256\":\"" + plan.tooling()
        + "\",\"wrapper_probe_id\":\"" + plan.bindings().get(WRAPPER).probeId()
        + "\",\"envelope_probe_id\":\"" + plan.bindings().get(ENVELOPE).probeId()
        + "\",\"rendezvous_probe_id\":\"" + plan.bindings().get(RENDEZVOUS).probeId()
        + "\",\"target_probe_id\":\"" + plan.bindings().get(TARGET).probeId()
        + "\",\"rejected_probes\":\"" + rejected + "\",\"max_scopes\":64,\"max_spans_per_scope\":1024"
        + (plan.route() == null ? "" : ",\"route_binding_sha256\":\"" + plan.route().sha256() + "\"")
        + ",\"scope\":" + (scope == null ? "null" : scope.json()) + "}\n";
  }

  /** Retain scoped outcomes after the SDK's bounded final drain; never invent a missing end. */
  public void finish(boolean queueKnown) {
    validCapture();
    String report;
    synchronized (this) {
      expire();
      for (WitnessScope scope : history.values()) {
        if (queueKnown && !"span_capacity".equals(scope.failure)) {
          scope.queueDropped = scope.spans.values().stream().filter(span -> span.ended && span.attempts == 0).count();
        }
        if (!scope.complete()) {
          scope.fail("process_exit");
          if (!Set.of(CANCELLED, EXPIRED, "invalidated", INCOMPLETE).contains(scope.phase)) scope.phase = "terminated";
        }
      }
      report = "{\"schema_version\":" + plan.version() + ",\"kind\":\"otelc.java-witness-final\",\"phase\":\"final\",\"pid\":"
          + ProcessHandle.current().pid() + ",\"producer\":" + reply(null, failure, null).strip()
          + ",\"queue_evidence\":\"" + (queueKnown ? "derived_after_shutdown" : "unavailable")
          + "\",\"scopes\":[" + String.join(",", history.values().stream().map(WitnessScope::json).toList()) + "]}\n";
    }
    try {
      Path parent = plan.socket().getParent();
      var permissions = java.nio.file.attribute.PosixFilePermissions.fromString("rwx------");
      if (!java.nio.file.Files.getPosixFilePermissions(parent).equals(permissions)) return;
      Path temporary = java.nio.file.Files.createTempFile(parent, ".witness-", ".json",
          java.nio.file.attribute.PosixFilePermissions.asFileAttribute(
              java.nio.file.attribute.PosixFilePermissions.fromString("rw-------")));
      try {
        java.nio.file.Files.writeString(temporary, report);
        java.nio.file.Files.move(temporary, parent.resolve("witness-health.json"),
            java.nio.file.StandardCopyOption.ATOMIC_MOVE, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
      } finally { java.nio.file.Files.deleteIfExists(temporary); }
    } catch (IOException unavailable) {
      // A consumer must reject absent final evidence; diagnostics never replace application output.
    }
  }

  private static boolean hex(String value, int size) { return value.length() == size && value.matches("[0-9a-f]+"); }
  private static String fresh() { return UUID.randomUUID().toString().replace("-", ""); }
}
