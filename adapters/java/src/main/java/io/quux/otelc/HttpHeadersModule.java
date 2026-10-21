package io.quux.otelc;

import static net.bytebuddy.matcher.ElementMatchers.named;
import static net.bytebuddy.matcher.ElementMatchers.isConstructor;

import io.opentelemetry.javaagent.extension.instrumentation.InstrumentationModule;
import io.opentelemetry.javaagent.extension.instrumentation.TypeInstrumentation;
import io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.util.List;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.asm.Advice.AssignReturned;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.matcher.ElementMatcher;

/** Preserve public JDK header views and inject the SDK carrier only into the private transport view. */
public final class HttpHeadersModule extends InstrumentationModule {
  public HttpHeadersModule() { super("otelc-http-headers"); }
  @Override public int order() { return -100; }
  @Override public List<String> getAdditionalHelperClassNames() {
    return List.of("io.quux.otelc.http.HeaderReadGuard", "io.quux.otelc.http.HeaderReadGuard$Carrier");
  }
  @Override public List<TypeInstrumentation> typeInstrumentations() {
    return List.of(new ImmutableRequest(), new TransportRequest());
  }

  private static final class ImmutableRequest implements TypeInstrumentation {
    @Override public ElementMatcher<TypeDescription> typeMatcher() {
      return named("jdk.internal.net.http.ImmutableHttpRequest");
    }
    @Override public void transform(TypeTransformer transformer) {
      transformer.applyAdviceToMethod(named("headers"), ImmutableHeadersAdvice.class.getName());
    }
  }

  private static final class TransportRequest implements TypeInstrumentation {
    @Override public ElementMatcher<TypeDescription> typeMatcher() {
      return named("jdk.internal.net.http.HttpRequestImpl");
    }
    @Override public void transform(TypeTransformer transformer) {
      transformer.applyAdviceToMethod(named("headers"), PublicTransportHeadersAdvice.class.getName());
      transformer.applyAdviceToMethod(isConstructor(), CaptureAdvice.class.getName());
      transformer.applyAdviceToMethod(named("getUserHeaders"), WireHeadersAdvice.class.getName());
    }
  }

  public static final class ImmutableHeadersAdvice {
    private ImmutableHeadersAdvice() {}
    @AssignReturned.ToReturned
    @Advice.OnMethodExit(suppress = Throwable.class)
    public static HttpHeaders exit(@Advice.FieldValue("headers") HttpHeaders original) {
      return original;
    }
  }

  public static final class PublicTransportHeadersAdvice {
    private PublicTransportHeadersAdvice() {}
    @AssignReturned.ToReturned
    @Advice.OnMethodExit(suppress = Throwable.class)
    public static HttpHeaders exit(@Advice.FieldValue("userHeaders") HttpHeaders original) {
      return original;
    }
  }

  public static final class CaptureAdvice {
    private CaptureAdvice() {}
    @Advice.OnMethodExit(suppress = Throwable.class)
    public static void exit(@Advice.This HttpRequest request) {
      io.quux.otelc.http.HeaderReadGuard.capture(request);
    }
  }

  public static final class WireHeadersAdvice {
    private WireHeadersAdvice() {}
    @AssignReturned.ToReturned
    @Advice.OnMethodExit(suppress = Throwable.class)
    public static HttpHeaders exit(@Advice.This HttpRequest request, @Advice.Return HttpHeaders headers) {
      return io.quux.otelc.http.HeaderReadGuard.wireHeaders(request, headers);
    }
  }
}
