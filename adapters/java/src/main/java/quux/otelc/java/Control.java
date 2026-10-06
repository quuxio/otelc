package quux.otelc.java;

import com.google.gson.Gson;
import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.Map;

/** Owner-only live metrics control, with bounded reads and owned-path cleanup. */
final class Control implements AutoCloseable {
  private final Telemetry runtime;
  private final Path filename;
  private final ServerSocketChannel server;
  private final Object inode;
  private volatile boolean closed;
  Control(Telemetry runtime) throws IOException {
    this.runtime = runtime;
    filename = runtime.plan.controlSocket().toAbsolutePath();
    var parent = filename.getParent();
    if (Files.isSymbolicLink(parent) || !Files.isDirectory(parent) || !Files.getOwner(parent).getName().equals(System.getProperty("user.name")) || !Files.getPosixFilePermissions(parent).equals(PosixFilePermissions.fromString("rwx------"))) throw new IOException("control directory must be owned by this user with mode 0700");
    if (Files.exists(filename)) throw new IOException("control socket path is occupied");
    server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
    try {
      server.bind(UnixDomainSocketAddress.of(filename));
      Files.setPosixFilePermissions(filename, PosixFilePermissions.fromString("rw-------"));
      inode = Files.getAttribute(filename, "unix:ino");
      Thread.ofPlatform().daemon().name("otelc-java-control").start(this::serve);
    } catch (IOException failure) { server.close(); throw failure; }
  }
  private void serve() {
    while (!closed) {
      try (var connection = server.accept()) {
        connection.configureBlocking(false);
        var request = ByteBuffer.allocate(18);
        long deadline = System.nanoTime() + 200_000_000L;
        boolean newline = false;
        while (System.nanoTime() < deadline && request.hasRemaining()) {
          int length = connection.read(request);
          if (length < 0) break;
          if (length == 0) { Thread.sleep(2); continue; }
          if (new String(request.array(), 0, request.position(), StandardCharsets.UTF_8).contains("\n")) { newline = true; break; }
        }
        String command = new String(request.array(), 0, request.position(), StandardCharsets.UTF_8);
        String response;
        if (!newline || !java.util.List.of("status\n", "enable\n", "disable\n").contains(command)) response = "{\"error\":\"invalid or incomplete control request\"}\n";
        else {
          if (!command.equals("status\n")) runtime.enabled = command.equals("enable\n");
          response = new Gson().toJson(Map.of("schema_version", 1, "pid", ProcessHandle.current().pid(), "metrics_enabled", runtime.enabled, "function_calls", runtime.count())) + "\n";
        }
        var bytes = ByteBuffer.wrap(response.getBytes(StandardCharsets.UTF_8));
        long writeDeadline = System.nanoTime() + 200_000_000L;
        while (bytes.hasRemaining() && System.nanoTime() < writeDeadline) { if (connection.write(bytes) == 0) Thread.sleep(2); }
      } catch (IOException ignored) { if (!closed) runtime.lose("invalid"); }
      catch (InterruptedException ignored) { Thread.currentThread().interrupt(); return; }
    }
  }
  @Override public void close() throws IOException {
    closed = true; server.close();
    if (Files.exists(filename) && Files.getAttribute(filename, "unix:ino").equals(inode)) Files.delete(filename);
  }
}
