package io.quux.otelc.methods;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.instrumentation.api.instrumenter.Instrumenter;
import io.opentelemetry.instrumentation.api.instrumenter.OperationListener;
import io.opentelemetry.instrumentation.api.annotation.support.async.AsyncOperationEndSupport;
import io.opentelemetry.instrumentation.api.incubator.semconv.code.CodeAttributesExtractor;
import io.opentelemetry.instrumentation.api.incubator.semconv.code.CodeAttributesGetter;
import io.opentelemetry.instrumentation.api.incubator.semconv.code.CodeSpanNameExtractor;
import java.util.concurrent.CompletionStage;
import java.lang.invoke.MethodType;

/** Only exception identity is observed; no application throwable methods are called. */
public final class Observer {
  private static final CodeAttributesGetter<Request> CODE = new CodeAttributesGetter<>() {
    @Override public Class<?> getCodeClass(Request request) { return request.owner(); }
    @Override public String getMethodName(Request request) { return request.method(); }
  };
  private static final Instrumenter<Request, Void> INSTRUMENTER = Instrumenter
      .<Request, Void>builder(GlobalOpenTelemetry.get(), "io.opentelemetry.methods", CodeSpanNameExtractor.create(CODE))
      .addAttributesExtractor(CodeAttributesExtractor.create(CODE))
      .setErrorCauseExtractor(error -> error)
      .addOperationListener(new OperationListener() {
        @Override public Context onStart(Context context, Attributes attributes, long nanos) {
          return context.with(new SafeSpan(Span.fromContext(context)));
        }
        @Override public void onEnd(Context context, Attributes attributes, long nanos) {
          // Completion and accounting remain with the official instrumenter.
        }
      }).buildInstrumenter();

  private Observer() {}

  public static Call enter(Class<?> owner, String method) {
    Request request = new Request(owner, method);
    Context parent = Context.current();
    if (!INSTRUMENTER.shouldStart(parent, request)) return null;
    Context context = INSTRUMENTER.start(parent, request);
    // Children need the original SDK span to inherit its anchored clock.
    // Keep the exception-safe wrapper only in the instrumenter's saved context.
    Span span = Span.fromContext(context);
    Context executing = span instanceof SafeSpan safe ? context.with(safe.original()) : context;
    return new Call(request, context, executing.makeCurrent());
  }

  public static Object exit(Call call, String descriptor, Object result, Throwable error) {
    if (call == null) return result;
    call.scope().close();
    if (result == null || error != null) {
      INSTRUMENTER.end(call.context(), call.request(), null, error);
      return result;
    }
    // Resolve only this return type. Reflecting a Method resolves unrelated declarations too.
    Class<?> returnType = MethodType.fromMethodDescriptorString(
        "()" + descriptor.substring(descriptor.indexOf(')') + 1), call.request().owner().getClassLoader()).returnType();
    Object observed = AsyncOperationEndSupport.create(INSTRUMENTER, Void.class, returnType)
        .asyncEnd(call.context(), call.request(), result, error);
    // Stage completion callbacks are installed eagerly; the application keeps its original future.
    return result instanceof CompletionStage<?> ? result : observed;
  }

  public record Request(Class<?> owner, String method) {}
  public record Call(Request request, Context context, Scope scope) {}
}
