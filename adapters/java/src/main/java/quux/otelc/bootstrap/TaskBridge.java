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
  public static volatile Hook hook;
  private TaskBridge() {}
  private static void failed(Hook owner) { try { owner.failed(); } catch (Throwable ignored) { /* Probes cannot replace application failures. */ } }
  public static void capture(Object task) { var owner=hook; if(owner!=null) try {owner.capture(task);} catch(Throwable ignored) {failed(owner);} }
  public static Object before(Object task) { var owner=hook; if(owner!=null) try {return owner.before(task);} catch(Throwable ignored) {failed(owner);} return null; }
  public static void after(Object scope) { var owner=hook; if(owner!=null && scope!=null) try {owner.after(scope);} catch(Throwable ignored) {failed(owner);} }
  public static void finished(Object task) {var owner=hook; if(owner!=null) try {owner.finished(task);} catch(Throwable ignored) {failed(owner);}}
  public static void complete(Object task) { var owner=hook; if(owner!=null) try {owner.complete(task);} catch(Throwable ignored) {failed(owner);} }
}
