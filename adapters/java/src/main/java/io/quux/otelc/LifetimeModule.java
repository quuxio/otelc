package io.quux.otelc;

import static net.bytebuddy.matcher.ElementMatchers.isConstructor;
import static net.bytebuddy.matcher.ElementMatchers.namedOneOf;
import static net.bytebuddy.matcher.ElementMatchers.isStatic;
import static net.bytebuddy.matcher.ElementMatchers.not;
import static net.bytebuddy.matcher.ElementMatchers.returns;
import static net.bytebuddy.matcher.ElementMatchers.takesArguments;

import io.opentelemetry.javaagent.extension.instrumentation.InstrumentationModule;
import io.opentelemetry.javaagent.extension.instrumentation.TypeInstrumentation;
import io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer;
import java.util.List;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.matcher.ElementMatcher;

/** Small lifecycle addition hosted by the official bytecode agent. */
public final class LifetimeModule extends InstrumentationModule {
  public LifetimeModule() { super("otelc-lifetimes"); }

  @Override public List<String> getAdditionalHelperClassNames() {
    return List.of("io.quux.otelc.lifecycle.Observer", "io.quux.otelc.lifecycle.Observer$Key",
        "io.quux.otelc.lifecycle.Observer$Tracked");
  }

  @Override public List<TypeInstrumentation> typeInstrumentations() {
    String types = System.getProperty("otelc.lifetime.types", "");
    if (types.isEmpty()) return List.of();
    return List.of(new TypeInstrumentation() {
      @Override public ElementMatcher<TypeDescription> typeMatcher() { return namedOneOf(types.split(",")); }
      @Override public void transform(TypeTransformer transformer) {
        transformer.applyAdviceToMethod(isConstructor(), Constructed.class.getName());
        transformer.applyAdviceToMethod(namedOneOf(System.getProperty("otelc.lifetime.release", "close").split(","))
            .and(not(isStatic())).and(takesArguments(0)).and(returns(void.class)), Released.class.getName());
      }
    });
  }

  public static final class Constructed {
    private Constructed() {}
    @Advice.OnMethodExit(suppress = Throwable.class)
    public static void exit(@Advice.This Object object, @Advice.Origin("#t") String type) {
      io.quux.otelc.lifecycle.Observer.constructed(object, type);
    }
  }

  public static final class Released {
    private Released() {}
    @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
    public static void exit(@Advice.This Object object, @Advice.Thrown Throwable error) {
      if (error == null) io.quux.otelc.lifecycle.Observer.released(object);
    }
  }
}
