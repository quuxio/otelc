import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Duration;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Ordinary HTTP client/server: business output and request headers are observable without telemetry APIs. */
public final class HttpContextApp {
  static Path audit;
  static String downstream;

  static void handle(HttpExchange exchange) throws IOException {
    String path = exchange.getRequestURI().getPath();
    String parent = exchange.getRequestHeaders().getFirst("traceparent");
    String baggage = exchange.getRequestHeaders().getFirst("baggage");
    Files.writeString(audit, path + "|" + parent + "|" + baggage + "\n",
        StandardOpenOption.CREATE, StandardOpenOption.APPEND);
    String value = path.equals("/error") ? "bad" : "8";
    if (path.equals("/relay")) {
      try (HttpClient client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()) {
        value = request(client, downstream).split(":")[1];
      } catch (Exception error) { throw new IOException(error); }
    }
    byte[] body = value.getBytes(StandardCharsets.UTF_8);
    exchange.getResponseHeaders().set("Content-Type", "text/plain");
    try {
      exchange.sendResponseHeaders(path.equals("/error") ? 500 : 200, body.length);
      exchange.getResponseBody().write(body);
    } finally { exchange.close(); }
  }

  static String request(HttpClient client, String target) throws Exception {
    HttpRequest request = HttpRequest.newBuilder(URI.create(target)).timeout(Duration.ofSeconds(10)).build();
    var originalHeaders = request.headers();
    int originalHash = request.hashCode();
    HttpRequest copied = HttpRequest.newBuilder(request, (name, value) -> true).build();
    boolean copiedHeaders = copied.headers().map().equals(originalHeaders.map());
    var response = client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    return response.statusCode() + ":" + response.body() + ":"
        + request.headers().firstValue("traceparent").orElse("-") + ":"
        + (request.headers() == originalHeaders) + ":" + (response.request() == request) + ":"
        + (request.hashCode() == originalHash) + ":" + copiedHeaders + ":"
        + response.request().headers().map().equals(originalHeaders.map());
  }

  public static void main(String[] args) throws Exception {
    BufferedReader input = new BufferedReader(new InputStreamReader(System.in));
    if (args[0].equals("server")) {
      audit = Path.of(args[1]);
      downstream = args.length > 2 ? args[2] : null;
      ExecutorService workers = Executors.newSingleThreadExecutor();
      HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
      server.setExecutor(workers);
      server.createContext("/", HttpContextApp::handle);
      server.start();
      System.out.println("ready:" + server.getAddress().getPort());
      try {
        for (String line; (line = input.readLine()) != null && !line.equals("quit");) {
          // A queue barrier runs after the preceding request and its server filter.
          workers.submit(() -> { /* No application work in the barrier. */ }).get();
          System.out.println("idle");
        }
      }
      finally { server.stop(0); workers.shutdown(); }
    } else {
      try (HttpClient client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()) {
        System.out.println("ready");
        for (String path; (path = input.readLine()) != null && !path.equals("quit");) {
          System.out.println(request(client, args[1] + path));
        }
      }
    }
  }
}
