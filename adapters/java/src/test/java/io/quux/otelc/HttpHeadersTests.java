package io.quux.otelc;

import static org.junit.jupiter.api.Assertions.*;

import java.net.URI;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.util.ArrayList;
import java.util.Map;
import net.bytebuddy.description.type.TypeDescription;
import org.junit.jupiter.api.Test;

class HttpHeadersTests {
  @Test void preservesTheOriginalPublicViewAndOnlyKeepsInjectedTransportHeaders() {
    HttpHeaders original = HttpHeaders.of(Map.of("existing", java.util.List.of("ordinary")), (key, value) -> true);
    HttpHeaders injected = HttpHeaders.of(Map.of("traceparent", java.util.List.of("wire")), (key, value) -> true);
    assertSame(original, HttpHeadersModule.ImmutableHeadersAdvice.exit(original));
    assertSame(original, HttpHeadersModule.PublicTransportHeadersAdvice.exit(original));
    HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost/")).build();
    HttpHeadersModule.CaptureAdvice.exit(request);
    assertSame(injected, HttpHeadersModule.WireHeadersAdvice.exit(request, injected));
  }

  @Test void matchesOnlyTheQualifiedJdkTypeAndRunsAfterTheOfficialGetterExit() throws Exception {
    var module = new HttpHeadersModule();
    assertEquals(-100, module.order());
    for (String helper : module.getAdditionalHelperClassNames()) {
      assertDoesNotThrow(() -> Class.forName(helper, false, getClass().getClassLoader()));
    }
    var type = module.typeInstrumentations().getFirst();
    var transport = module.typeInstrumentations().getLast();
    Class<?> request = HttpRequest.newBuilder(URI.create("http://localhost/")).build().getClass();
    assertTrue(type.typeMatcher().matches(new TypeDescription.ForLoadedType(request)));
    assertFalse(type.typeMatcher().matches(new TypeDescription.ForLoadedType(String.class)));
    assertTrue(transport.typeMatcher().matches(new TypeDescription.ForLoadedType(Class.forName(
        "jdk.internal.net.http.HttpRequestImpl", false, ClassLoader.getPlatformClassLoader()))));
    assertFalse(transport.typeMatcher().matches(new TypeDescription.ForLoadedType(request)));
    var names = new ArrayList<String>();
    var transformer = (io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer)
        java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(),
            new Class<?>[] {io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer.class},
            (proxy, method, arguments) -> {
              if (!method.getName().equals("applyAdviceToMethod")) throw new AssertionError(method.getName());
              names.add((String) arguments[1]);
              return null;
            });
    type.transform(transformer);
    transport.transform(transformer);
    assertEquals(java.util.List.of(HttpHeadersModule.ImmutableHeadersAdvice.class.getName(),
        HttpHeadersModule.PublicTransportHeadersAdvice.class.getName(),
        HttpHeadersModule.CaptureAdvice.class.getName(), HttpHeadersModule.WireHeadersAdvice.class.getName()), names);
  }
}
