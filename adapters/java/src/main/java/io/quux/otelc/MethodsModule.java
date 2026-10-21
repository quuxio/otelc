package io.quux.otelc;

import static net.bytebuddy.matcher.ElementMatchers.namedOneOf;

import io.opentelemetry.javaagent.extension.instrumentation.InstrumentationModule;
import io.opentelemetry.javaagent.extension.instrumentation.TypeInstrumentation;
import io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer;
import java.util.ArrayList;
import java.util.List;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.matcher.ElementMatcher;
import net.bytebuddy.implementation.bytecode.assign.Assigner;

/** Function hooks hosted by the official agent, without application exception callbacks. */
public final class MethodsModule extends InstrumentationModule {
  public MethodsModule() { super("otelc-methods"); }

  @Override public List<String> getAdditionalHelperClassNames() {
    return List.of("io.quux.otelc.methods.Observer", "io.quux.otelc.methods.Observer$Call",
        "io.quux.otelc.methods.Observer$Request", "io.quux.otelc.methods.Observer$1",
        "io.quux.otelc.methods.Observer$2", "io.quux.otelc.methods.SafeSpan");
  }

  @Override public List<TypeInstrumentation> typeInstrumentations() {
    List<TypeInstrumentation> result = new ArrayList<>();
    String selection = System.getProperty("otel.instrumentation.methods.include", "");
    for (String entry : selection.split(";")) {
      if (entry.isEmpty()) continue;
      int bracket = entry.indexOf('[');
      String owner = entry.substring(0, bracket);
      String[] methods = entry.substring(bracket + 1, entry.length() - 1).split(",");
      result.add(new TypeInstrumentation() {
        @Override public ElementMatcher<TypeDescription> typeMatcher() { return namedOneOf(owner); }
        @Override public void transform(TypeTransformer transformer) {
          transformer.applyAdviceToMethod(namedOneOf(methods), MethodAdvice.class.getName());
        }
      });
    }
    return result;
  }

  public static final class MethodAdvice {
    private MethodAdvice() {}
    @Advice.OnMethodEnter(suppress = Throwable.class)
    public static io.quux.otelc.methods.Observer.Call enter(
        @Advice.Origin Class<?> owner, @Advice.Origin("#m") String method) {
      return io.quux.otelc.methods.Observer.enter(owner, method);
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
    public static void exit(@Advice.Enter io.quux.otelc.methods.Observer.Call call,
                            @Advice.Origin java.lang.reflect.Method method,
                            @Advice.Return(readOnly = false, typing = Assigner.Typing.DYNAMIC) Object result,
                            @Advice.Thrown Throwable error) {
      result = io.quux.otelc.methods.Observer.exit(call, method.getReturnType(), result, error);
    }
  }
}
