package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;
import org.junit.jupiter.api.Test;

class FrameHierarchyTest {
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
}
