package io.quux.otelc.witness;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class WitnessV2Tests {
  @Test void scopeRetainsTheLogicalThreadAtEnvelopeEntry() throws Exception {
    Thread original = Thread.ofPlatform().unstarted(() -> {});
    WitnessScope scope = new WitnessScope("a".repeat(32), 1, original, Long.MAX_VALUE);
    String expected = "\"thread_id\":\"" + original.threadId() + "\"";
    assertTrue(scope.json().contains(expected), scope.json());
    // The control thread is different; its identity must not replace the owner.
    var observed = new java.util.concurrent.atomic.AtomicReference<String>();
    Thread other = Thread.ofPlatform().start(() -> observed.set(scope.json()));
    other.join();
    assertTrue(observed.get().contains(expected), observed.get());
  }
}
