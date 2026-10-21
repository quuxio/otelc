package io.quux.otelc.witness;

import io.opentelemetry.javaagent.bootstrap.InstrumentationHolder;
import io.opentelemetry.javaagent.extension.AgentListener;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdk;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizer;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizerProvider;
import java.io.IOException;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.ProtectionDomain;
import java.util.HashMap;
import java.util.Map;
import java.util.Properties;
import org.objectweb.asm.ClassWriter;

/** Test-only SPI: the production launcher does not enable a partial witness capability. */
public final class WitnessCaptureTestExtension implements AutoConfigurationCustomizerProvider, AgentListener {
  private static final String OWNER = "witness/App";
  private static final String TARGET = "witness/Work";
  private static final Properties REPORT = new Properties();
  private static WitnessClassCapture capture;
  private static Instrumentation instrumentation;

  @Override public void customize(AutoConfigurationCustomizer ignored) {
    try {
      instrumentation = InstrumentationHolder.getInstrumentation();
      Path classes = Path.of(System.getProperty("otelc.test.classes"));
      byte[] original = Files.readAllBytes(classes.resolve(OWNER + ".class"));
      byte[] target = Files.readAllBytes(classes.resolve(TARGET + ".class"));
      Map<String, WitnessBindings.Declaration> declarations = new HashMap<>();
      for (String role : java.util.List.of("envelope", "rendezvous", "wrapper", "target")) {
        declarations.put(role, new WitnessBindings.Declaration(role.equals("target") ? TARGET : OWNER,
            role, role.equals("rendezvous") ? "()V" : "(I)I"));
      }
      Map<String, WitnessBindings.Binding> bindings = WitnessBindings.compile(Map.of(OWNER, original, TARGET, target), declarations);
      REPORT.setProperty("expected_class", WitnessBindings.digest(original));
      REPORT.setProperty("expected_method", bindings.get("target").methodSha256());
      if (mode("tamper")) instrumentation.addTransformer(new Tamper(), true);
      if (mode("preloaded")) Class.forName("witness.App", false, ClassLoader.getSystemClassLoader());
      Path codeSource = Path.of(System.getProperty("otelc.test.classpath"));
      String origin = (mode("origin") ? classes.resolve("wrong") : codeSource).toUri().toURL().toExternalForm();
      capture = WitnessClassCapture.install(instrumentation, bindings, Map.of(OWNER, origin, TARGET, origin));
    } catch (IOException | ClassNotFoundException error) {
      throw new IllegalStateException(error);
    } catch (IllegalStateException rejected) {
      REPORT.setProperty("rejected", rejected.getMessage());
    }
  }

  @Override public void afterAgent(AutoConfiguredOpenTelemetrySdk ignored) {
    try {
      instrumentation.addTransformer(new Downstream(), true);
      Class<?> owner = Class.forName("witness.App", false, ClassLoader.getSystemClassLoader());
      Class<?> target = Class.forName("witness.Work", false, ClassLoader.getSystemClassLoader());
      REPORT.setProperty("initialised_before_main", Boolean.toString(Files.exists(Path.of(System.getProperty("otelc.test.marker")))));
      if (mode("retransform")) instrumentation.retransformClasses(owner);
      if (capture != null) {
        Map<String, WitnessClassCapture.Loaded> identities = capture.verifyLoaded();
        WitnessClassCapture.Loaded loaded = identities.get(OWNER);
        REPORT.setProperty("captured_class", loaded.classSha256());
        REPORT.setProperty("loaded_origin", loaded.origin());
        REPORT.setProperty("loaded_identity", Boolean.toString(loaded.type() == owner && identities.get(TARGET).type() == target));
      }
    } catch (ReflectiveOperationException | java.lang.instrument.UnmodifiableClassException error) {
      throw new IllegalStateException(error);
    } catch (IllegalStateException rejected) {
      REPORT.setProperty("rejected", rejected.getMessage());
    } finally {
      try (var output = Files.newOutputStream(Path.of(System.getProperty("otelc.test.report")))) {
        REPORT.store(output, "actual pinned agent input capture");
      } catch (IOException error) {
        throw new IllegalStateException(error);
      }
    }
  }

  private static boolean mode(String value) { return value.equals(System.getProperty("otelc.test.mode")); }

  public static final class Tamper implements ClassFileTransformer {
    @Override public byte[] transform(Module module, ClassLoader loader, String name, Class<?> redefined,
                                     ProtectionDomain domain, byte[] bytes) {
      if (!OWNER.equals(name)) return null;
      var type = new org.objectweb.asm.tree.ClassNode();
      new org.objectweb.asm.ClassReader(bytes).accept(type, 0);
      type.sourceFile = "changed-before-capture.java";
      ClassWriter writer = new ClassWriter(0);
      type.accept(writer);
      return writer.toByteArray();
    }
  }

  public static final class Downstream implements ClassFileTransformer {
    @Override public byte[] transform(Module module, ClassLoader loader, String name, Class<?> redefined,
                                     ProtectionDomain domain, byte[] bytes) {
      if (!TARGET.equals(name)) return null;
      REPORT.setProperty("observed_origin", domain.getCodeSource().getLocation().toExternalForm());
      var declaration = new WitnessBindings.Declaration(TARGET, "target", "(I)I");
      REPORT.setProperty("downstream_method", WitnessBindings.methodDigest(declaration,
          WitnessBindings.find(WitnessBindings.read(bytes), declaration)));
      return null;
    }
  }
}
