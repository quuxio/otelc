package quux.otelc.java;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.commons.AdviceAdapter;
import org.objectweb.asm.commons.Method;

/** In-memory JDK method probes preserve original queue entries and Future identities. */
final class TaskWeaver implements ClassFileTransformer {
  private static final Type BRIDGE=Type.getObjectType("quux/otelc/bootstrap/TaskBridge");
  private static final Method CAPTURE=new Method("capture","(Ljava/lang/Object;)V");
  private static final Method COMPLETE=new Method("complete","(Ljava/lang/Object;)V");
  private static final Method FINISHED=new Method("finished","(Ljava/lang/Object;)V");
  private static final Method BEFORE=new Method("before","(Ljava/lang/Object;)Ljava/lang/Object;");
  private static final Method AFTER=new Method("after","(Ljava/lang/Object;)V");
  boolean transformedPool;
  boolean transformedFuture;
  byte[] weave(String owner, byte[] original) {
    boolean pool=owner.equals("java/util/concurrent/ThreadPoolExecutor");
    boolean future=owner.equals("java/util/concurrent/FutureTask");
    if(!pool && !future) return null;
    var reader=new ClassReader(original); var writer=new ClassWriter(reader,ClassWriter.COMPUTE_FRAMES|ClassWriter.COMPUTE_MAXS);
    reader.accept(new ClassVisitor(Opcodes.ASM9,writer) {
      @Override public MethodVisitor visitMethod(int access,String name,String descriptor,String signature,String[] exceptions) {
        var output=super.visitMethod(access,name,descriptor,signature,exceptions);
        boolean submission=pool && name.equals("execute") && descriptor.equals("(Ljava/lang/Runnable;)V");
        boolean execution=future && name.equals("run") && descriptor.equals("()V");
        boolean completion=future && name.equals("done") && descriptor.equals("()V");
        boolean cancellation=future && name.equals("cancel") && descriptor.equals("(Z)Z");
        if(!submission && !execution && !completion && !cancellation) return output;
        return new AdviceAdapter(Opcodes.ASM9,output,access,name,descriptor) {
          private final Label start=new Label(), end=new Label(), handler=new Label();
          private int scope;
          @Override protected void onMethodEnter() {
            if(submission) {loadArg(0); invokeStatic(BRIDGE,CAPTURE);}
            if(execution) {loadThis(); invokeStatic(BRIDGE,BEFORE); scope=newLocal(Type.getType(Object.class)); storeLocal(scope);}
            mark(start);
          }
          private void restore() {loadLocal(scope); invokeStatic(BRIDGE,AFTER);}
          @Override protected void onMethodExit(int opcode) {
            if(opcode==ATHROW) return;
            if(execution) {restore();loadThis();invokeStatic(BRIDGE,FINISHED);}
            if(completion) {loadThis();invokeStatic(BRIDGE,COMPLETE);}
            if(cancellation) {var unchanged=new Label(); dup(); ifZCmp(EQ,unchanged); loadThis();invokeStatic(BRIDGE,COMPLETE);mark(unchanged);}
          }
          @Override public void visitMaxs(int stack,int locals) {
            if(submission || execution) {
              mark(end);visitTryCatchBlock(start,end,handler,"java/lang/Throwable");mark(handler);
              if(submission) {loadArg(0);invokeStatic(BRIDGE,COMPLETE);} else {restore();loadThis();invokeStatic(BRIDGE,FINISHED);}
              throwException();
            }
            super.visitMaxs(stack,locals);
          }
        };
      }
    },ClassReader.EXPAND_FRAMES);
    return writer.toByteArray();
  }
  @Override public byte[] transform(Module module,ClassLoader loader,String name,Class<?> redefining,ProtectionDomain domain,byte[] original) {
    var result=weave(name==null?"":name,original);
    if(result!=null) {if(name.endsWith("ThreadPoolExecutor")) transformedPool=true; else transformedFuture=true;}
    return result;
  }
}
