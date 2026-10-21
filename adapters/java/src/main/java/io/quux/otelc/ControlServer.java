package io.quux.otelc;

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
import java.time.Duration;

/** Bounded local control, isolated from application sockets and stdout. */
final class ControlServer {
  private ControlServer() {}

  static ServerSocketChannel start(Path socket, LiveState state) throws IOException {
    if (!Files.getPosixFilePermissions(socket.getParent()).equals(PosixFilePermissions.fromString("rwx------"))) {
      throw new IOException("control directory must be private (0700)");
    }
    ServerSocketChannel listener = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
    try {
      listener.bind(UnixDomainSocketAddress.of(socket));
      Files.setPosixFilePermissions(socket, PosixFilePermissions.fromString("rw-------"));
    } catch (IOException error) {
      listener.close();
      throw error;
    }
    Thread worker = Thread.ofPlatform().daemon().name("otelc-control").unstarted(() -> {
      while (listener.isOpen()) {
        try (SocketChannel channel = listener.accept()) { serve(channel, state); }
        catch (IOException ignored) { /* EOF/timeout affects this request only. */ }
      }
    });
    worker.start();
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      try { listener.close(); Files.deleteIfExists(socket); }
      catch (IOException ignored) { /* The owning process is already terminating. */ }
    }, "otelc-control-cleanup"));
    return listener;
  }

  static void serve(SocketChannel channel, LiveState state) throws IOException {
    channel.configureBlocking(false);
    String command = readCommand(channel);
    String response = command == null ? "{\"error\":\"incomplete control request\"}\n" : state.command(command);
    ByteBuffer bytes = StandardCharsets.UTF_8.encode(response);
    long writeDeadline = System.nanoTime() + Duration.ofMillis(200).toNanos();
    while (bytes.hasRemaining() && System.nanoTime() < writeDeadline) channel.write(bytes);
  }

  private static String readCommand(SocketChannel channel) throws IOException {
    long deadline = System.nanoTime() + Duration.ofMillis(200).toNanos();
    ByteBuffer input = ByteBuffer.allocate(65);
    while (System.nanoTime() < deadline && input.hasRemaining()) {
      int count = channel.read(input);
      if (count < 0) return null;
      if (count > 0 && input.get(input.position() - 1) == '\n') {
        String value = new String(input.array(), 0, input.position() - 1, StandardCharsets.US_ASCII);
        return value.indexOf('\n') < 0 ? value : null;
      }
      java.util.concurrent.locks.LockSupport.parkNanos(1_000_000);
    }
    return null;
  }
}
