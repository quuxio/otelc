package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonParser;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import javax.tools.ToolProvider;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class FrameHierarchyTest {
  @TempDir Path directory;
  private FrameTypes hierarchy(ClassLoader loader) throws Exception {
    try(var input=getClass().getClassLoader().getResourceAsStream("quux/otelc/java/FrameHierarchyTest.class")) {
      return new FrameTypes(new org.objectweb.asm.ClassReader(input),loader);
    }
  }
  @Test void metadataJoinsInterfacesAndArraysWithoutDefiningApplicationClasses() throws Exception {
    var loader=new ClassLoader(getClass().getClassLoader()) {
      @Override protected Class<?> loadClass(String name,boolean resolve) throws ClassNotFoundException {
        throw new AssertionError("frame computation must not load a class: "+name);
      }
    };
    var types=hierarchy(loader);
    assertEquals("java/lang/String",types.common("java/lang/String","java/lang/String"));
    assertEquals("java/lang/Number",types.common("java/lang/Integer","java/lang/Double"));
    assertEquals("java/util/List",types.common("java/util/List","java/util/ArrayList"));
    assertEquals("java/util/List",types.common("java/util/ArrayList","java/util/List"));
    assertEquals("java/lang/Object",types.common("java/util/List","java/io/Serializable"));
    assertEquals("java/lang/Object",types.common("java/lang/String","java/lang/StringBuilder"));
    assertEquals("[Ljava/lang/Number;",types.common("[Ljava/lang/Integer;","[Ljava/lang/Double;"));
    assertEquals("[Ljava/lang/Object;",types.common("[[I","[[J"));
    assertEquals("[I",types.common("[I","[I"));
    assertEquals("java/lang/Object",types.common("[I","[J"));
    assertEquals("java/lang/Object",types.common("[I","java/lang/String"));
    assertEquals("java/lang/Cloneable",types.common("[I","java/lang/Cloneable"));
    assertEquals("java/io/Serializable",types.common("java/io/Serializable","[I"));
    assertEquals("[Ljava/lang/Object;",types.common("[[I","[Ljava/lang/Object;"));
    assertEquals("java/lang/Object",types.common("[Ljava/lang/String;","[I"));
    assertEquals("[[Ljava/lang/Number;",types.common("[[Ljava/lang/Integer;","[[Ljava/lang/Double;"));
    // One-letter class names are legal; they are not primitive descriptors.
    var writer=new org.objectweb.asm.ClassWriter(0);
    writer.visit(65,org.objectweb.asm.Opcodes.ACC_PUBLIC,"I",null,"java/lang/Number",null);writer.visitEnd();
    var single=new FrameTypes(new org.objectweb.asm.ClassReader(writer.toByteArray()),loader);
    assertEquals("java/lang/Number",single.common("I","java/lang/Integer"));
    assertThrows(IllegalArgumentException.class,()->types.common("unavailable/Type","java/lang/String"));
    var wrong=new ClassLoader(getClass().getClassLoader()) {
      @Override public java.io.InputStream getResourceAsStream(String name) {
        return getParent().getResourceAsStream("java/lang/String.class");
      }
    };
    assertThrows(IllegalArgumentException.class,()->hierarchy(wrong).common("unavailable/Type","java/lang/Integer"));
    var broken=new ClassLoader() {
      @Override public java.io.InputStream getResourceAsStream(String name) {
        return new java.io.InputStream(){@Override public int read() throws java.io.IOException {throw new java.io.IOException("failed metadata read");}};
      }
    };
    assertThrows(IllegalArgumentException.class,()->hierarchy(broken).common("unavailable/Type","java/lang/String"));
    assertEquals("java/lang/Number",hierarchy(null).common("java/lang/Integer","java/lang/Double"));
  }
  @Test void inFlightClassHierarchyKeepsBothBranchOutcomesAndTelemetry() throws Exception {
    String source="""
      public class Main {
        static class Child extends Main {}
        Main selected(boolean flag) {
          Main value; if(flag)value=this;else value=new Child();return value;
        }
        public static void main(String[] args) {
          System.out.println(new Main().selected(Boolean.parseBoolean(args[0])).getClass().getSimpleName());
        }
      }
      """;
    Path file=directory.resolve("Main.java");Files.writeString(file,source);
    assertEquals(0,ToolProvider.getSystemJavaCompiler().run(null,null,null,"-g","-d",directory.toString(),file.toString()));
    Path agent=Path.of("target/java-agent-0.1.0-agent.jar").toAbsolutePath();
    try(var receiver=new AgentTest.Receiver()) {
      var policy=AgentTest.policy(receiver.endpoint());
      policy.getAsJsonObject("function_matchers").getAsJsonArray("include").set(0,new com.google.gson.JsonPrimitive("(?-u)^Main\\.selected.*"));
      Path plan=directory.resolve("plan.json");Files.writeString(plan,policy.toString());
      for(String flag:List.of("true","false")) {
        for(boolean sourceLaunch:List.of(false,true)) {
          Path report=directory.resolve("report.json");
          var target=sourceLaunch?List.of("Main.java",flag):List.of("-cp",directory.toString(),"Main",flag);
          var plain=new java.util.ArrayList<String>();plain.add(Path.of(System.getProperty("java.home"),"bin/java").toString());plain.addAll(target);
          var original=new ProcessBuilder(plain).directory(directory.toFile()).redirectErrorStream(true).start();
          byte[] output=original.getInputStream().readAllBytes();assertEquals(0,original.waitFor());
          var measured=new java.util.ArrayList<String>();measured.add(plain.getFirst());measured.add("-javaagent:"+agent+"="+plan);measured.addAll(target);
          var builder=new ProcessBuilder(measured).directory(directory.toFile());builder.environment().put("OTELC_REPORT_PATH",report.toString());
          var process=builder.start();byte[] actual=process.getInputStream().readAllBytes();String errors=new String(process.getErrorStream().readAllBytes(),java.nio.charset.StandardCharsets.UTF_8);
          assertEquals(0,process.waitFor(),errors);assertArrayEquals(output,actual);assertEquals(source,Files.readString(file));
          var health=JsonParser.parseString(Files.readString(report)).getAsJsonObject();
          assertEquals(1,health.get("function_calls").getAsInt(),errors);
          assertEquals(0,health.get("export_loss").getAsInt());assertEquals("",errors);
        }
      }
    }
  }
}
