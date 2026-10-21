package io.quux.otelc.witness;

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

/** Independent bounded protocol; metrics commands and their limits remain unchanged. */
final class WitnessServer {
  private WitnessServer() {}

  static void start(Path socket, WitnessRuntime runtime) throws IOException {
    if (!Files.getPosixFilePermissions(socket.getParent()).equals(PosixFilePermissions.fromString("rwx------"))) {
      throw new IOException("witness directory must be private");
    }
    ServerSocketChannel listener = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
    try {
      listener.bind(UnixDomainSocketAddress.of(socket));
      Files.setPosixFilePermissions(socket, PosixFilePermissions.fromString("rw-------"));
    } catch (IOException error) {
      listener.close(); throw error;
    }
    Thread.ofPlatform().daemon().name("otelc-witness-control").start(() -> {
      while (listener.isOpen()) {
        try (var channel = listener.accept()) { serve(channel, runtime); }
        catch (IOException ignored) { /* A disconnected peer does not retire another action. */ }
      }
    });
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      try { listener.close(); Files.deleteIfExists(socket); }
      catch (IOException ignored) { /* The owning JVM is terminating. */ }
    }, "otelc-witness-cleanup"));
  }

  static void serve(SocketChannel channel, WitnessRuntime runtime) throws IOException {
    channel.configureBlocking(false);
    ByteBuffer input = ByteBuffer.allocate(2049);
    long deadline = System.nanoTime() + Duration.ofMillis(200).toNanos();
    String command = null;
    while (input.hasRemaining() && System.nanoTime() < deadline) {
      int count = channel.read(input);
      if (count < 0) break;
      if (count > 0 && input.get(input.position() - 1) == '\n') {
        String value = new String(input.array(), 0, input.position() - 1, StandardCharsets.US_ASCII);
        if (value.matches("[a-z0-9 ]{1,2048}")) command = value;
        break;
      }
      java.util.concurrent.locks.LockSupport.parkNanos(1_000_000);
    }
    ByteBuffer response = StandardCharsets.US_ASCII.encode(runtime.command(command == null ? "invalid" : command));
    deadline = System.nanoTime() + Duration.ofMillis(500).toNanos();
    while (response.hasRemaining() && System.nanoTime() < deadline) {
      channel.write(response);
      if (response.hasRemaining()) java.util.concurrent.locks.LockSupport.parkNanos(1_000_000);
    }
  }
}
