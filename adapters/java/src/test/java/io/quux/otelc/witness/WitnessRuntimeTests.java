package io.quux.otelc.witness;

import static net.bytebuddy.matcher.ElementMatchers.hasDescriptor;
import static net.bytebuddy.matcher.ElementMatchers.named;
import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import io.quux.otelc.MethodsModule;
import java.lang.instrument.Instrumentation;
import java.lang.invoke.MethodHandles;
import java.lang.reflect.Proxy;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;
import net.bytebuddy.ByteBuddy;
import net.bytebuddy.asm.Advice;
import net.bytebuddy.dynamic.ClassFileLocator;
import net.bytebuddy.pool.TypePool;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;

class WitnessRuntimeTests {
  @TempDir Path root;

  private static final class Exporter implements SpanExporter {
    final List<SpanData> spans = new ArrayList<>();
    final List<CompletableResultCode> pending = new ArrayList<>();
    boolean delay;
    boolean fail;
    @Override public CompletableResultCode export(Collection<SpanData> batch) {
      spans.addAll(batch);
      CompletableResultCode result = delay ? new CompletableResultCode()
          : fail ? CompletableResultCode.ofFailure() : CompletableResultCode.ofSuccess();
      if (delay) pending.add(result);
      return result;
    }
    @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
    @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
  }

  @Test void actualAdviceOwnershipControlAndExporterCallbacks() throws Exception {
    String owner = "io/quux/otelc/witness/GeneratedAction";
    byte[] original = fixture(owner);
    Map<String, WitnessBindings.Declaration> declarations = new HashMap<>();
    for (String role : WitnessPlan.ROLES) declarations.put(role,
        new WitnessBindings.Declaration(owner, role, role.equals("rendezvous") ? "()V" : "(I)I"));
    var bindings = WitnessBindings.compile(Map.of(owner, original), declarations);
    var domain = getClass().getProtectionDomain();
    var defined = new ArrayList<Class<?>>();
    Instrumentation instrumentation = (Instrumentation) Proxy.newProxyInstance(getClass().getClassLoader(),
        new Class<?>[] {Instrumentation.class}, (proxy, method, args) -> switch (method.getName()) {
          case "getAllLoadedClasses" -> defined.toArray(Class<?>[]::new);
          case "addTransformer" -> null;
          case "removeTransformer" -> true;
          default -> throw new AssertionError(method.getName());
        });
    String origin = domain.getCodeSource().getLocation().toExternalForm();
    var plan = new WitnessPlan(root.resolve("witness.sock"), bindings, Map.of(owner, origin),
        "a".repeat(64), "b".repeat(64), "c".repeat(64), "d".repeat(64));
    try (var capture = WitnessClassCapture.install(instrumentation, bindings, plan.origins())) {
      capture.transform(null, getClass().getClassLoader(), owner, null, domain, original);
      var clock = new java.util.concurrent.atomic.AtomicLong(System.nanoTime());
      var runtime = new WitnessRuntime(plan, capture, clock::get);
      try (var locator = new ClassFileLocator.Compound(ClassFileLocator.Simple.of(owner.replace('/', '.'), original),
          ClassFileLocator.ForClassLoader.ofSystemLoader())) {
        var builder = new ByteBuddy().redefine(TypePool.Default.of(locator).describe(owner.replace('/', '.')).resolve(), locator);
        for (var binding : bindings.values()) builder = builder.visit(Advice.withCustomMapping()
            .bind(MethodsModule.Probe.class, binding.probeId()).bind(MethodsModule.Measured.class, false)
            .to(MethodsModule.RoleAdvice.class).on(named(binding.declaration().name()).and(hasDescriptor(binding.declaration().descriptor()))));
        byte[] transformed = builder.visit(new WitnessMethodGuard(new ArrayList<>(bindings.values()), runtime::guarded)).make().getBytes();
        Class<?> actual = MethodHandles.lookup().defineClass(transformed);
        defined.add(actual);
        Exporter exporter = new Exporter();
        try (var provider = SdkTracerProvider.builder().addSpanProcessor(WitnessPipeline.processor(
            SimpleSpanProcessor.create(WitnessPipeline.exporter(exporter, runtime)), runtime)).build()) {
          runtime.attach(OpenTelemetrySdk.builder().setTracerProvider(provider).build());
          assertTrue(exchange(root.resolve("witness.sock"), "1 " + "0".repeat(32) + " status\n").contains("\"scope\":null"));
          assertTrue(exchange(root.resolve("witness.sock"), "unfinished").contains("invalid_request"));
          assertTrue(exchange(root.resolve("witness.sock"), "x".repeat(2049) + "\n").contains("invalid_request"));
          assertThrows(java.io.IOException.class, () -> WitnessServer.start(root.resolve("witness.sock"), runtime));
          String identity = property(command(runtime, "status"), "runtime_id");
          var method = actual.getMethod("envelope", int.class);
          var callback = actual.getField("CALLBACK");
          var armed = new ArrayList<String>();
          Thread ownerThread = Thread.currentThread();
          var nested = new java.util.concurrent.atomic.AtomicBoolean();
          callback.set(null, (Runnable) () -> {
            if (Thread.currentThread() != ownerThread || nested.get()) return;
            String status = command(runtime, "status");
            assertTrue(status.contains("\"phase\":\"ready\""), status);
            String[] arguments = armArguments(status, plan);
            for (int index : new int[] {0, 1, 2, 5, 6, 7, 8, 9}) {
              String[] wrong = arguments.clone();
              wrong[index] = index == 2 ? "999" : "0".repeat(wrong[index].length());
              assertTrue(command(runtime, "arm " + String.join(" ", wrong)).contains("arm_identity_mismatch"));
            }
            String ack = command(runtime, "arm " + String.join(" ", arguments));
            assertTrue(ack.contains("\"phase\":\"armed\""), ack);
            armed.add(property(ack, "scope_id"));
            assertTrue(command(runtime, "arm " + String.join(" ", arguments)).contains("not_ready"));
            assertTrue(command(runtime, "end " + identity + " " + armed.getLast()).contains("root_active"));
            // An unrelated reflective wrapper call must not consume or poison this envelope's arm.
            try { assertEquals(100, actual.getMethod("wrapper", int.class).invoke(null, 99)); }
            catch (ReflectiveOperationException error) { throw new AssertionError(error); }
            nested.set(true);
            try {
              assertEquals(100, method.invoke(null, 99));
              var otherFailure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
              Thread noise = Thread.ofPlatform().start(() -> {
                try { assertEquals(101, method.invoke(null, 100)); }
                catch (ReflectiveOperationException | AssertionError error) { otherFailure.set(error); }
              });
              noise.join(2000);
              assertFalse(noise.isAlive());
              assertNull(otherFailure.get());
              assertTrue(command(runtime, "status").contains("\"phase\":\"armed\""));
            } catch (ReflectiveOperationException | InterruptedException error) { throw new AssertionError(error); }
            finally { nested.set(false); }
          });
          assertEquals(8, method.invoke(null, 7));
          String first = command(runtime, "end " + identity + " " + armed.getLast());
          assertTrue(first.contains("\"producer_complete\":true"), first);
          assertEquals(2, exporter.spans.size());
          callback.set(null, (Runnable) () -> {
            String ack = command(runtime, "arm " + String.join(" ", armArguments(command(runtime, "status"), plan)));
            armed.add(property(ack, "scope_id"));
          });
          var error = assertThrows(java.lang.reflect.InvocationTargetException.class, () -> method.invoke(null, -1));
          assertInstanceOf(IllegalArgumentException.class, error.getCause());
          assertTrue(command(runtime, "end " + identity + " " + armed.getLast()).contains("\"producer_complete\":true"));
          assertTrue(exporter.spans.getLast().getStatus().getStatusCode() == io.opentelemetry.api.trace.StatusCode.ERROR);
          exporter.delay = true;
          assertEquals(3, method.invoke(null, 2));
          String pending = command(runtime, "end " + identity + " " + armed.getLast());
          assertTrue(pending.contains("\"producer_complete\":false"), pending);
          exporter.pending.forEach(CompletableResultCode::succeed);
          String recovered = command(runtime, "end " + identity + " " + armed.getLast());
          assertTrue(recovered.contains("\"producer_complete\":true"), recovered);
          assertTrue(recovered.contains("\"flush_failures\":\"1\""), recovered);
          exporter.delay = false; exporter.fail = true;
          assertEquals(4, method.invoke(null, 3));
          String failed = command(runtime, "end " + identity + " " + armed.getLast());
          assertTrue(failed.contains("\"producer_complete\":false"), failed);
          assertTrue(failed.contains("\"export_failures\":\"2\""), failed);
          exporter.fail = false;
          callback.set(null, (Runnable) () -> {
            String ack = command(runtime, "arm " + String.join(" ", armArguments(command(runtime, "status"), plan)));
            armed.add(property(ack, "scope_id"));
            assertTrue(command(runtime, "cancel " + identity + " " + armed.getLast()).contains("cancelled"));
          });
          assertEquals(5, method.invoke(null, 4));
          assertTrue(command(runtime, "end " + identity + " " + armed.getLast()).contains("\"producer_complete\":false"));
          callback.set(null, (Runnable) () -> {
            String ack = command(runtime, "arm " + String.join(" ", armArguments(command(runtime, "status"), plan)));
            armed.add(property(ack, "scope_id"));
          });
          assertEquals(13, method.invoke(null, 12));
          String recursive = command(runtime, "end " + identity + " " + armed.getLast());
          assertTrue(recursive.contains("\"producer_complete\":true"), recursive);
          assertTrue(recursive.contains("\"target_entries\":\"3\""), recursive);
          callback.set(null, (Runnable) () -> {
            String[] arguments = armArguments(command(runtime, "status"), plan);
            clock.addAndGet(java.util.concurrent.TimeUnit.SECONDS.toNanos(31));
            assertTrue(command(runtime, "arm " + String.join(" ", arguments)).contains("not_ready"));
          });
          assertEquals(6, method.invoke(null, 5));
          assertTrue(command(runtime, "status").contains("\"failure\":\"expired\""));
          assertTrue(runtime.command("invalid").contains("invalid_request"));
          assertTrue(command(runtime, "status bad id").contains("invalid_request"));
          assertTrue(command(runtime, "unknown").contains("unknown_operation"));
          assertTrue(command(runtime, "end " + identity + " " + "0".repeat(32)).contains("unknown_scope"));
          assertTrue(command(runtime, "cancel " + identity + " " + "0".repeat(32)).contains("unknown_scope"));
          var previous = io.opentelemetry.api.trace.SpanContext.create(property(first, "trace_id"),
              property(first, "root_span_id"), io.opentelemetry.api.trace.TraceFlags.getSampled(),
              io.opentelemetry.api.trace.TraceState.getDefault());
          provider.get("late-control").spanBuilder("late")
              .setParent(io.opentelemetry.context.Context.root().with(io.opentelemetry.api.trace.Span.wrap(previous)))
              .setAttribute(WitnessRuntime.SCOPE, property(first, "scope_id")).startSpan().end();
          String invalidated = command(runtime, "status " + identity + " " + property(first, "scope_id"));
          assertTrue(invalidated.contains("\"phase\":\"invalidated\""), invalidated);
          assertTrue(invalidated.contains("\"failure\":\"late_span\""), invalidated);
          assertTrue(invalidated.contains("\"producer_complete\":false"), invalidated);
          capture.transform(null, getClass().getClassLoader(), owner, actual, domain, original);
          assertTrue(command(runtime, "arm").contains("class_identity_invalid"));
          runtime.finish(false);
          assertTrue(java.nio.file.Files.readString(root.resolve("witness-health.json")).contains("class_identity_invalid"));
          callback.set(null, null);
        }
      }
    }
  }

  private static String exchange(Path socket, String request) throws Exception {
    try (var channel = java.nio.channels.SocketChannel.open(java.net.UnixDomainSocketAddress.of(socket))) {
      channel.write(java.nio.charset.StandardCharsets.US_ASCII.encode(request));
      channel.configureBlocking(false);
      var input = java.nio.ByteBuffer.allocate(16384);
      long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(3);
      while (System.nanoTime() < deadline && input.hasRemaining()) {
        if (channel.read(input) < 0 || input.position() > 0 && input.get(input.position() - 1) == '\n') break;
        java.util.concurrent.locks.LockSupport.parkNanos(1_000_000);
      }
      return new String(input.array(), 0, input.position(), java.nio.charset.StandardCharsets.US_ASCII);
    }
  }

  private static String command(WitnessRuntime runtime, String operation) {
    return runtime.command("1 " + UUID.randomUUID().toString().replace("-", "") + " " + operation);
  }

  private static String property(String json, String key) {
    var matcher = Pattern.compile("\"" + key + "\":\"([a-z0-9]+)\"").matcher(json);
    assertTrue(matcher.find(), json);
    return matcher.group(1);
  }

  private static String[] armArguments(String status, WitnessPlan plan) {
    return new String[] {property(status, "runtime_id"), property(status, "cookie"), property(status, "sequence"),
        "e".repeat(64), "f".repeat(64), plan.build(), plan.selection(), plan.tooling(),
        plan.bindings().get("wrapper").probeId(), plan.policy()};
  }

  private static byte[] fixture(String owner) {
    ClassWriter writer = new ClassWriter(ClassWriter.COMPUTE_FRAMES | ClassWriter.COMPUTE_MAXS);
    writer.visit(Opcodes.V21, Opcodes.ACC_PUBLIC, owner, null, "java/lang/Object", null);
    writer.visitField(Opcodes.ACC_PUBLIC | Opcodes.ACC_STATIC, "CALLBACK", "Ljava/lang/Runnable;", null, null).visitEnd();
    for (String name : WitnessPlan.ROLES) {
      var method = writer.visitMethod(Opcodes.ACC_PUBLIC | Opcodes.ACC_STATIC, name, name.equals("rendezvous") ? "()V" : "(I)I", null, null);
      method.visitCode();
      switch (name) {
        case "rendezvous" -> {
          method.visitFieldInsn(Opcodes.GETSTATIC, owner, "CALLBACK", "Ljava/lang/Runnable;");
          method.visitMethodInsn(Opcodes.INVOKEINTERFACE, "java/lang/Runnable", "run", "()V", true);
          method.visitInsn(Opcodes.RETURN);
        }
        case "envelope", "wrapper" -> {
          if (name.equals("envelope")) method.visitMethodInsn(Opcodes.INVOKESTATIC, owner, "rendezvous", "()V", false);
          method.visitVarInsn(Opcodes.ILOAD, 0);
          method.visitMethodInsn(Opcodes.INVOKESTATIC, owner, name.equals("envelope") ? "wrapper" : "target", "(I)I", false);
          method.visitInsn(Opcodes.IRETURN);
        }
        case "target" -> {
          org.objectweb.asm.Label positive = new org.objectweb.asm.Label();
          method.visitVarInsn(Opcodes.ILOAD, 0); method.visitJumpInsn(Opcodes.IFGE, positive);
          method.visitTypeInsn(Opcodes.NEW, "java/lang/IllegalArgumentException"); method.visitInsn(Opcodes.DUP);
          method.visitMethodInsn(Opcodes.INVOKESPECIAL, "java/lang/IllegalArgumentException", "<init>", "()V", false);
          method.visitInsn(Opcodes.ATHROW); method.visitLabel(positive);
          var small = new org.objectweb.asm.Label();
          method.visitVarInsn(Opcodes.ILOAD, 0); method.visitIntInsn(Opcodes.BIPUSH, 10);
          method.visitJumpInsn(Opcodes.IF_ICMPLE, small);
          method.visitVarInsn(Opcodes.ILOAD, 0); method.visitInsn(Opcodes.ICONST_1); method.visitInsn(Opcodes.ISUB);
          method.visitMethodInsn(Opcodes.INVOKESTATIC, owner, "target", "(I)I", false);
          method.visitInsn(Opcodes.ICONST_1); method.visitInsn(Opcodes.IADD); method.visitInsn(Opcodes.IRETURN);
          method.visitLabel(small);
          method.visitVarInsn(Opcodes.ILOAD, 0); method.visitInsn(Opcodes.ICONST_1); method.visitInsn(Opcodes.IADD);
          method.visitInsn(Opcodes.IRETURN);
        }
        default -> throw new AssertionError(name);
      }
      method.visitMaxs(0, 0); method.visitEnd();
    }
    writer.visitEnd(); return writer.toByteArray();
  }
}
