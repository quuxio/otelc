package quux.otelc.java;

import quux.otelc.bootstrap.TaskBridge;

/** Loaded only after bootstrap dispatch installation to preserve loader identity. */
public final class TaskBinding implements TaskBridge.Hook {
  private final TaskContext context;
  private TaskBinding(TaskContext context) {this.context=context;}
  public static void bind(Object context) {
    if(TaskBridge.class.getClassLoader()!=null) throw new IllegalArgumentException("Java task bridge must be bootstrap loaded");
    if(TaskBridge.hook!=null) throw new IllegalArgumentException("Java task context is already installed");
    TaskBridge.hook=new TaskBinding((TaskContext)context);
  }
  public static void unbind(Object context) {
    if(TaskBridge.hook instanceof TaskBinding binding && binding.context==context) TaskBridge.hook=null;
  }
  @Override public void capture(Object task) {context.capture(task);}
  @Override public Object before(Object task) {return context.before(task);}
  @Override public void after(Object scope) {context.after(scope);}
  @Override public void complete(Object task) {context.complete(task);}
  @Override public void finished(Object task) {context.finished(task);}
  @Override public void failed() {context.failed();}
}
