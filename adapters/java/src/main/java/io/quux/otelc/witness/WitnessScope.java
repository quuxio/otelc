package io.quux.otelc.witness;

import java.util.LinkedHashMap;
import java.util.Map;

/** Bounded metadata only; all access is under the owning runtime's monitor. */
final class WitnessScope {
  static final int MAX_SPANS = 1024;
  final String cookie;
  final long sequence;
  final java.lang.ref.WeakReference<Thread> thread;
  final long expires;
  final Map<String, SpanState> spans = new LinkedHashMap<>();
  String id;
  String caseId;
  String attempt;
  String phase = "entered";
  String failure;
  String trace;
  String root;
  String target;
  boolean envelopeExited;
  boolean rootExited;
  boolean targetExited;
  int targetEntries;
  long exportFailures;
  long flushFailures;
  boolean checkpoint;
  Long queueDropped;

  WitnessScope(String cookie, long sequence, Thread thread, long expires) {
    this.cookie = cookie;
    this.sequence = sequence;
    this.thread = new java.lang.ref.WeakReference<>(thread);
    this.expires = expires;
  }

  static final class SpanState {
    boolean ended;
    boolean exported;
    int inFlight;
    long attempts;
  }

  void fail(String reason) {
    if (failure == null) failure = reason;
  }

  boolean complete() {
    return "complete".equals(phase) && canComplete();
  }

  boolean canComplete() {
    return checkpoint && failure == null && (queueDropped == null || queueDropped == 0)
        && rootExited && envelopeExited && targetExited && targetEntries >= 1
        && root != null && target != null && !spans.isEmpty()
        && spans.values().stream().allMatch(span -> span.ended && span.exported && span.inFlight == 0);
  }

  String json() {
    return "{\"cookie\":\"" + cookie + "\",\"sequence\":\"" + sequence + "\",\"scope_id\":" + value(id)
        + ",\"case_id\":" + value(caseId) + ",\"attempt_id\":" + value(attempt)
        + ",\"phase\":\"" + phase + "\",\"failure\":" + value(failure)
        + ",\"trace_id\":" + value(trace) + ",\"root_span_id\":" + value(root)
        + ",\"target_span_id\":" + value(target)
        + ",\"root_exited\":" + rootExited + ",\"envelope_exited\":" + envelopeExited
        + ",\"target_exited\":" + targetExited + ",\"target_entries\":\"" + targetEntries
        + "\",\"export_failures\":\"" + exportFailures + "\",\"flush_failures\":\"" + flushFailures
        + "\",\"checkpoint\":" + checkpoint + ",\"producer_complete\":" + complete()
        + ",\"receiver_confirmation_required\":true,\"started_ids\":" + ids(0)
        + ",\"ended_ids\":" + ids(1) + ",\"exported_ids\":" + ids(2)
        + ",\"pending_exports\":\"" + spans.values().stream().mapToInt(span -> span.inFlight).sum()
        + "\",\"queue_dropped\":" + (queueDropped == null ? "null" : "\"" + queueDropped + "\"") + "}";
  }

  private String ids(int state) {
    return "[" + String.join(",", spans.entrySet().stream().filter(entry -> state == 0
        || state == 1 && entry.getValue().ended || state == 2 && entry.getValue().exported)
        .map(entry -> "\"" + entry.getKey() + "\"").toList()) + "]";
  }

  // All protocol values are fixed literals or validated lowercase hexadecimal IDs.
  static String value(String text) { return text == null ? "null" : "\"" + text + "\""; }
}
