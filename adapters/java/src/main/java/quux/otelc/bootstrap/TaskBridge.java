package quux.otelc.bootstrap;

/** Bootstrap-visible dispatch only; all bounded state belongs to the launch runtime. */
public final class TaskBridge {
  public interface Hook {
    void capture(Object task);
    Object before(Object task);
    void after(Object scope);
    void complete(Object task);
    void finished(Object task);
    void failed();
  }
  private record Scope(Hook owner,Object value) {}
  public static volatile Hook hook;
  private TaskBridge() {}
  private static void failed(Hook owner) { try { owner.failed(); } catch (Throwable ignored) { /* Probes cannot replace application failures. */ } }
  public static void capture(Object task) { var owner=hook; if(owner!=null) try {owner.capture(task);} catch(Throwable ignored) {failed(owner);} }
  public static Object before(Object task) { var owner=hook; if(owner!=null) try {var value=owner.before(task);return value==null?null:new Scope(owner,value);} catch(Throwable ignored) {failed(owner);} return null; }
  public static void after(Object value) { if(value instanceof Scope scope) try {scope.owner().after(scope.value());} catch(Throwable ignored) {failed(scope.owner());} }
  public static void finished(Object task) {var owner=hook; if(owner!=null) try {owner.finished(task);} catch(Throwable ignored) {failed(owner);}}
  public static void complete(Object task) { var owner=hook; if(owner!=null) try {owner.complete(task);} catch(Throwable ignored) {failed(owner);} }
}
