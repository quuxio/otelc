package io.quux.otelc;

import static net.bytebuddy.matcher.ElementMatchers.namedOneOf;
import static net.bytebuddy.matcher.ElementMatchers.named;
import static net.bytebuddy.matcher.ElementMatchers.hasDescriptor;
import static net.bytebuddy.matcher.ElementMatchers.none;
import static net.bytebuddy.matcher.ElementMatchers.not;

import io.opentelemetry.javaagent.extension.instrumentation.InstrumentationModule;
import io.opentelemetry.javaagent.extension.instrumentation.TypeInstrumentation;
import io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer;
import java.util.ArrayList;
import java.util.List;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.description.method.MethodDescription;
import net.bytebuddy.matcher.ElementMatcher;
import net.bytebuddy.implementation.bytecode.assign.Assigner;

/** Function hooks hosted by the official agent, without application exception callbacks. */
public final class MethodsModule extends InstrumentationModule {
  public MethodsModule() { super("otelc-methods"); }

  @Override public List<String> getAdditionalHelperClassNames() {
    var helpers = new ArrayList<>(List.of("io.quux.otelc.methods.Observer", "io.quux.otelc.methods.Observer$Call",
        "io.quux.otelc.methods.Observer$Request", "io.quux.otelc.methods.Observer$1",
        "io.quux.otelc.methods.Observer$2", "io.quux.otelc.methods.SafeSpan"));
    if (io.quux.otelc.witness.WitnessRuntime.get() != null) helpers.add("io.quux.otelc.witness.probe.Observer");
    return helpers;
  }

  @Override public List<TypeInstrumentation> typeInstrumentations() {
    java.util.Map<String, List<String>> selections = new java.util.LinkedHashMap<>();
    String selection = System.getProperty("otel.instrumentation.methods.include", "");
    for (String entry : selection.split(";")) {
      if (entry.isEmpty()) continue;
      int bracket = entry.indexOf('[');
      String owner = entry.substring(0, bracket);
      List<String> methods = List.of(entry.substring(bracket + 1, entry.length() - 1).split(","));
      selections.put(owner, methods);
    }
    var witness = io.quux.otelc.witness.WitnessRuntime.get();
    if (witness != null) for (String owner : witness.owners()) selections.putIfAbsent(owner, List.of());
    return selections.entrySet().stream().<TypeInstrumentation>map(selected ->
        new Selected(selected.getKey(), selected.getValue(), witness)).toList();
  }

  private record Selected(String owner, List<String> methods, io.quux.otelc.witness.WitnessRuntime witness)
      implements TypeInstrumentation {
    @Override public ElementMatcher<TypeDescription> typeMatcher() { return namedOneOf(owner); }
    @Override public void transform(TypeTransformer transformer) {
      var bindings = witness == null ? List.<io.quux.otelc.witness.WitnessBindings.Binding>of() : witness.bindings(owner);
      ElementMatcher.Junction<MethodDescription> roleMethods = none();
      for (var binding : bindings) {
        var matcher = named(binding.declaration().name()).and(hasDescriptor(binding.declaration().descriptor()));
        roleMethods = roleMethods.or(matcher);
        boolean measured = methods.contains(binding.declaration().name());
        transformer.applyAdviceToMethod(matcher, mapping -> mapping.bind(Probe.class, binding.probeId())
            .bind(Measured.class, measured), RoleAdvice.class.getName());
      }
      if (!methods.isEmpty()) transformer.applyAdviceToMethod(namedOneOf(methods.toArray(String[]::new)).and(not(roleMethods)), MethodAdvice.class.getName());
      if (!bindings.isEmpty()) transformer.applyTransformer((builder, type, loader, module, domain) ->
          builder.visit(new io.quux.otelc.witness.WitnessMethodGuard(bindings, witness::guarded)));
    }
  }

  @java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.RUNTIME)
  @java.lang.annotation.Target(java.lang.annotation.ElementType.PARAMETER)
  public @interface Probe {}

  @java.lang.annotation.Retention(java.lang.annotation.RetentionPolicy.RUNTIME)
  @java.lang.annotation.Target(java.lang.annotation.ElementType.PARAMETER)
  public @interface Measured {}

  public static final class RoleAdvice {
    private RoleAdvice() {}
    @Advice.OnMethodEnter(suppress = Throwable.class)
    public static Object enter(@Advice.Origin Class<?> owner, @Advice.Origin("#m") String method,
        @Probe String probe, @Measured boolean measured,
        @Advice.Local("otelcFunction") io.quux.otelc.methods.Observer.Call function) {
      Object token = io.quux.otelc.witness.probe.Observer.enter(owner, probe);
      // ByteBuddy writes this parameter into the shared local read by exit advice.
      if (measured) function = io.quux.otelc.methods.Observer.enter(owner, method);
      return token;
    }

    @Advice.OnMethodExit(onThrowable = Throwable.class, suppress = Throwable.class)
    public static void exit(@Advice.Enter Object token,
        @Advice.Local("otelcFunction") io.quux.otelc.methods.Observer.Call function,
        @Advice.Origin("#d") String descriptor,
        @Advice.Return(readOnly = false, typing = Assigner.Typing.DYNAMIC) Object result,
        @Advice.Thrown Throwable error) {
      try {
        if (function != null) result = io.quux.otelc.methods.Observer.exit(function, descriptor, result, error);
      }
      finally { io.quux.otelc.witness.probe.Observer.exit(token, error); }
    }
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
                            @Advice.Origin("#d") String descriptor,
                            @Advice.Return(readOnly = false, typing = Assigner.Typing.DYNAMIC) Object result,
                            @Advice.Thrown Throwable error) {
      result = io.quux.otelc.methods.Observer.exit(call, descriptor, result, error);
    }
  }
}
