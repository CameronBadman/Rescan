package dev.rescan.common;

import com.fasterxml.jackson.databind.*;
import java.net.URI;
import java.net.http.*;
import java.sql.Timestamp;
import java.time.Duration;
import java.util.*;
import java.util.function.*;

/** SQL-over-HTTP; uncertain writes are never transparently replayed. */
public class TursoDb {
  private final URI endpoint;
  private final Supplier<String> token;
  private final ObjectMapper json = new ObjectMapper();
  private final HttpClient http =
      HttpClient.newBuilder()
          .version(HttpClient.Version.HTTP_1_1)
          .connectTimeout(Duration.ofSeconds(3))
          .build();

  private static final class Session {
    String baton;
    URI route;
    long deadline;
  }

  private final ThreadLocal<Session> session = new ThreadLocal<>();

  public TursoDb(String url, Supplier<String> token) {
    endpoint =
        URI.create(
            url.replaceFirst("^(libsql|turso):", "https:").replaceAll("/$", "") + "/v2/pipeline");
    if (!"https".equals(endpoint.getScheme())
        && !Set.of("localhost", "127.0.0.1", "libsql").contains(endpoint.getHost()))
      throw new IllegalArgumentException("Turso requires TLS outside local development");
    this.token = token;
  }

  private JsonNode send(com.fasterxml.jackson.databind.node.ObjectNode body) {
    try {
      var active = session.get();
      var req =
          HttpRequest.newBuilder(active != null && active.route != null ? active.route : endpoint)
              .timeout(Duration.ofSeconds(4))
              .header("Content-Type", "application/json");
      String credential = token.get();
      if (!credential.isEmpty()) req.header("Authorization", "Bearer " + credential);
      var response =
          http.send(
              req.POST(HttpRequest.BodyPublishers.ofString(body.toString())).build(),
              HttpResponse.BodyHandlers.ofString());
      if (response.statusCode() != 200)
        throw new IllegalStateException("Turso HTTP " + response.statusCode());
      var root = json.readTree(response.body());
      if (active != null) {
        active.baton = root.path("baton").isTextual() ? root.path("baton").asText() : null;
        if (root.path("base_url").isTextual()) {
          URI base = URI.create(root.path("base_url").asText());
          boolean same =
              Objects.equals(base.getHost(), endpoint.getHost())
                  && Objects.equals(base.getScheme(), endpoint.getScheme())
                  && base.getPort() == endpoint.getPort();
          boolean turso =
              "https".equals(base.getScheme())
                  && endpoint.getHost().endsWith(".turso.io")
                  && base.getHost() != null
                  && base.getHost().endsWith(".turso.io")
                  && base.getPort() == -1;
          if ((!same && !turso) || base.getUserInfo() != null)
            throw new IllegalStateException("Unexpected database routing origin");
          active.route = URI.create(base.toString().replaceAll("/$", "") + "/v2/pipeline");
        }
      }
      for (var r : root.path("results"))
        if ("error".equals(r.path("type").asText()))
          throw new IllegalStateException(
              "Turso SQL error: " + r.path("error").path("code").asText("UNKNOWN"));
      return root;
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new IllegalStateException("Turso interrupted", e);
    } catch (java.io.IOException e) {
      throw new IllegalStateException(
          "Turso response unavailable; write outcome may be unknown", e);
    }
  }

  private JsonNode run(String sql, Object... args) {
    var s = session.get();
    if (s != null && System.nanoTime() > s.deadline)
      throw new IllegalStateException("Transaction deadline exceeded");
    var body = json.createObjectNode();
    if (s != null && s.baton != null) body.put("baton", s.baton);
    var requests = body.putArray("requests");
    if (s == null || s.baton == null)
      requests
          .addObject()
          .put("type", "execute")
          .putObject("stmt")
          .put("sql", "PRAGMA foreign_keys=ON");
    var stmt = requests.addObject().put("type", "execute").putObject("stmt");
    stmt.put("sql", sql).put("want_rows", true);
    var values = stmt.putArray("args");
    for (Object value : args) {
      var v = values.addObject();
      if (value == null) v.put("type", "null");
      else if (value instanceof Number || value instanceof Timestamp || value instanceof Boolean) {
        long n =
            value instanceof Timestamp t
                ? t.getTime() / 1000
                : value instanceof Boolean b ? (b ? 1 : 0) : ((Number) value).longValue();
        v.put("type", "integer").put("value", Long.toString(n));
      } else v.put("type", "text").put("value", value.toString());
    }
    int index = requests.size() - 1;
    if (s == null) requests.addObject().put("type", "close");
    return send(body).path("results").get(index).path("response").path("result");
  }

  public List<Map<String, Object>> queryForList(String sql, Object... args) {
    var result = run(sql, args);
    var rows = new ArrayList<Map<String, Object>>();
    for (var row : result.path("rows")) {
      var mapped = new LinkedHashMap<String, Object>();
      for (int i = 0; i < row.size(); i++) {
        String name = result.path("cols").get(i).path("name").asText();
        var cell = row.get(i);
        Object value =
            switch (cell.path("type").asText()) {
              case "null" -> null;
              case "integer" -> cell.path("value").asLong();
              case "float" -> cell.path("value").asDouble();
              default -> cell.path("value").asText();
            };
        if (value instanceof String str
            && (name.equals("id") || name.endsWith("_id") || name.equals("token"))) {
          try {
            value = UUID.fromString(str);
          } catch (IllegalArgumentException ignored) {
          }
        }
        if (value instanceof Long n
            && (name.endsWith("_at") || name.endsWith("_until") || name.equals("empty_since")))
          value = new Timestamp(n * 1000);
        mapped.put(name, value);
      }
      rows.add(mapped);
    }
    return rows;
  }

  @SuppressWarnings("unchecked")
  public <T> T queryForObject(String sql, Class<T> type, Object... args) {
    var rows = queryForList(sql, args);
    if (rows.size() != 1) throw new IllegalStateException("Expected one row");
    Object v = rows.getFirst().values().iterator().next();
    if (v == null) return null;
    if (type == Integer.class) v = ((Number) v).intValue();
    if (type == Long.class) v = ((Number) v).longValue();
    if (type == Boolean.class) v = ((Number) v).longValue() != 0;
    return (T) v;
  }

  public int update(String sql, Object... args) {
    return run(sql, args).path("affected_row_count").asInt();
  }

  public void execute(String sql) {
    run(sql);
  }

  public void batchUpdate(String sql, List<Object[]> rows) {
    if (rows.isEmpty()) return;
    int split = sql.toUpperCase(Locale.ROOT).lastIndexOf("VALUES");
    if (split < 0) throw new IllegalArgumentException("Bulk insert required");
    var expressions = new ArrayList<String>();
    for (int i = 0; i < rows.getFirst().length; i++)
      expressions.add("json_extract(value,'$[" + i + "]')");
    try {
      update(
          sql.substring(0, split)
              + " SELECT "
              + String.join(",", expressions)
              + " FROM json_each(?)",
          json.writeValueAsString(rows));
    } catch (java.io.IOException e) {
      throw new IllegalArgumentException(e);
    }
  }

  public <T> T execute(Function<Object, T> work) {
    if (session.get() != null) throw new IllegalStateException("Nested transaction");
    var s = new Session();
    s.deadline = System.nanoTime() + Duration.ofMillis(4500).toNanos();
    session.set(s);
    try {
      run("BEGIN IMMEDIATE");
      T value = work.apply(null);
      run("COMMIT");
      return value;
    } catch (RuntimeException e) {
      s.deadline = Long.MAX_VALUE;
      try {
        if (s.baton != null) run("ROLLBACK");
      } catch (RuntimeException ignored) {
      }
      throw e;
    } finally {
      if (s.baton != null) {
        var body = json.createObjectNode().put("baton", s.baton);
        body.putArray("requests").addObject().put("type", "close");
        try {
          send(body);
        } catch (RuntimeException ignored) {
        }
      }
      session.remove();
    }
  }

  public void executeWithoutResult(Consumer<Object> work) {
    execute(
        tx -> {
          work.accept(tx);
          return null;
        });
  }

  public void migrate() {
    try {
      String ddl =
          new String(
              Objects.requireNonNull(getClass().getResourceAsStream("/db/turso/V1.sql"))
                  .readAllBytes(),
              java.nio.charset.StandardCharsets.UTF_8);
      String checksum =
          HexFormat.of()
              .formatHex(
                  java.security.MessageDigest.getInstance("SHA-256")
                      .digest(ddl.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
      execute(
          "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT"
              + " NOT NULL)");
      executeWithoutResult(
          tx -> {
            var found = queryForList("SELECT checksum FROM schema_migrations WHERE version=1");
            if (!found.isEmpty()) {
              if (!checksum.equals(found.getFirst().get("checksum")))
                throw new IllegalStateException("Migration checksum mismatch");
              return;
            }
            for (String statement : ddl.split(";")) if (!statement.isBlank()) execute(statement);
            update("INSERT INTO schema_migrations VALUES(1,?)", checksum);
          });
    } catch (java.io.IOException | java.security.NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }
}
