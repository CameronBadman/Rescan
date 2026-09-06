package dev.rescan.common;

import static org.junit.jupiter.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.*;
import org.junit.jupiter.api.Test;

class TursoHttpTest {
  @Test
  void ambiguousCommitNeverReplaysMutation() throws Exception {
    var statements = new ArrayList<String>();
    var json = new ObjectMapper();
    var server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    server.createContext(
        "/v2/pipeline",
        exchange -> {
          var input = json.readTree(exchange.getRequestBody());
          var output = json.createObjectNode().put("baton", "test-baton");
          var results = output.putArray("results");
          boolean uncertain = false;
          for (var request : input.path("requests")) {
            String sql = request.path("stmt").path("sql").asText();
            statements.add(sql);
            uncertain |= "COMMIT".equals(sql);
            var response = results.addObject().put("type", "ok").putObject("response");
            response.put("type", request.path("type").asText());
            response.putObject("result").put("affected_row_count", 1).putArray("rows");
          }
          byte[] bytes = output.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8);
          exchange.sendResponseHeaders(uncertain ? 503 : 200, bytes.length);
          exchange.getResponseBody().write(bytes);
          exchange.close();
        });
    server.start();
    try {
      var db = new TursoDb("http://127.0.0.1:" + server.getAddress().getPort(), () -> "");
      assertThrows(
          IllegalStateException.class,
          () -> db.executeWithoutResult(tx -> db.update("INSERT INTO test VALUES(1)")));
      assertEquals(1, Collections.frequency(statements, "INSERT INTO test VALUES(1)"));
      assertEquals(1, Collections.frequency(statements, "COMMIT"));
    } finally {
      server.stop(0);
    }
  }

  @Test
  void remotePlaintextIsRejected() {
    assertThrows(
        IllegalArgumentException.class, () -> new TursoDb("http://example.com", () -> "secret"));
  }
}
