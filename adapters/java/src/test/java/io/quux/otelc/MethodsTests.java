package io.quux.otelc;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.quux.otelc.methods.SafeSpan;
import java.util.List;
import java.util.concurrent.TimeUnit;
import net.bytebuddy.description.type.TypeDescription;
import org.junit.jupiter.api.Test;

class MethodsTests {
  @Test void safeExceptionEventAndAllDelegationKeepTheSdkSpan() {
    var exporter = InMemorySpanExporter.create();
    try (var provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(exporter)).build()) {
      Span original = provider.get("safe-exception-test").spanBuilder("original").startSpan();
      Span safe = new SafeSpan(original);
      Throwable error = new RuntimeException() {
        @Override public String getMessage() { throw new AssertionError("message called"); }
        @Override public String toString() { throw new AssertionError("format called"); }
        @Override public synchronized Throwable getCause() { throw new AssertionError("cause called"); }
      };
      assertTrue(safe.isRecording());
      assertEquals(original.getSpanContext(), safe.getSpanContext());
      assertSame(safe, safe.setAttribute(AttributeKey.longKey("count"), 3L));
      assertSame(safe, safe.setStatus(StatusCode.ERROR, "safe status"));
      assertSame(safe, safe.updateName("renamed"));
      assertSame(safe, safe.addLink(original.getSpanContext(), Attributes.empty()));
      assertSame(safe, safe.addEvent("first", Attributes.empty()));
      assertSame(safe, safe.addEvent("second", Attributes.empty(), System.currentTimeMillis(), TimeUnit.MILLISECONDS));
      safe.recordException(null);
      safe.recordException(error);
      safe.end();
      var data = exporter.getFinishedSpanItems().getFirst();
      assertEquals("renamed", data.getName());
      assertEquals(3L, data.getAttributes().get(AttributeKey.longKey("count")));
      assertEquals(3, data.getEvents().size());
      assertEquals(Attributes.of(AttributeKey.stringKey("exception.type"), error.getClass().getName()),
          data.getEvents().get(2).getAttributes());
      assertEquals(1, data.getLinks().size());
      new SafeSpan(provider.get("test").spanBuilder("timestamped").startSpan())
          .end(System.currentTimeMillis(), TimeUnit.MILLISECONDS);
      assertEquals(2, exporter.getFinishedSpanItems().size());
    }
  }

  @Test void generatedSelectionInstallsOnlySelectedOwnersAndNames() {
    String property = "otel.instrumentation.methods.include";
    String previous = System.getProperty(property);
    try {
      System.clearProperty(property);
      MethodsModule module = new MethodsModule();
      assertTrue(module.typeInstrumentations().isEmpty());
      for (String helper : module.getAdditionalHelperClassNames()) {
        assertDoesNotThrow(() -> Class.forName(helper, false, getClass().getClassLoader()));
      }
      System.setProperty(property, "java.lang.String[length,isEmpty];java.lang.Integer[intValue]");
      var types = module.typeInstrumentations();
      assertEquals(2, types.size());
      assertTrue(types.getFirst().typeMatcher().matches(new TypeDescription.ForLoadedType(String.class)));
      assertFalse(types.getFirst().typeMatcher().matches(new TypeDescription.ForLoadedType(Integer.class)));
      var names = new java.util.ArrayList<String>();
      var transformer = (io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer)
          java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(),
              new Class<?>[] {io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer.class},
              (proxy, method, arguments) -> {
                if (!method.getName().equals("applyAdviceToMethod")) throw new AssertionError(method.getName());
                names.add((String) arguments[1]);
                return null;
              });
      for (var type : types) type.transform(transformer);
      assertEquals(List.of(MethodsModule.MethodAdvice.class.getName(), MethodsModule.MethodAdvice.class.getName()), names);
    } finally {
      if (previous == null) System.clearProperty(property); else System.setProperty(property, previous);
    }
  }
}
