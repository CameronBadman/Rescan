package dev.rescan.common;

import io.lettuce.core.*;
import io.lettuce.core.api.StatefulRedisConnection;
import java.time.Duration;
import java.util.*;

public class Queue implements AutoCloseable {
  public static final String GROUP = "workers";
  public final String STREAM, DEAD;
  private final RedisClient client;
  private final StatefulRedisConnection<String, String> connection;

  public Queue() {
    this(redisUrl());
  }

  private static String redisUrl() {
    return Secrets.get("REDIS_URL", "REDIS_SECRET_ARN");
  }

  public Queue(String url) {
    this(url, "rescan");
  }

  public Queue(String url, String prefix) {
    if (!prefix.matches("rescan[-a-z0-9]*"))
      throw new IllegalArgumentException("Invalid queue prefix");
    STREAM = prefix + ":documents";
    DEAD = prefix + ":dead";
    var address = java.net.URI.create(url);
    if (!"rediss".equals(address.getScheme())
        && !Set.of("localhost", "127.0.0.1", "redis").contains(address.getHost()))
      throw new IllegalArgumentException("Remote Redis requires TLS");
    client = RedisClient.create(url);
    client.setDefaultTimeout(Duration.ofSeconds(10));
    connection = client.connect();
    try {
      connection
          .sync()
          .xgroupCreate(
              XReadArgs.StreamOffset.from(STREAM, "0-0"),
              GROUP,
              XGroupCreateArgs.Builder.mkstream());
    } catch (RedisCommandExecutionException error) {
      if (!error.getMessage().contains("BUSYGROUP")) throw error;
    }
  }

  public void publish(UUID document) {
    connection
        .sync()
        .xadd(
            STREAM,
            XAddArgs.Builder.maxlen(100000).approximateTrimming(),
            Map.of("documentId", document.toString()));
  }

  public void publishMany(List<UUID> documents) {
    var futures = new ArrayList<io.lettuce.core.RedisFuture<String>>();
    for (UUID id : documents)
      futures.add(
          connection
              .async()
              .xadd(
                  STREAM,
                  XAddArgs.Builder.maxlen(100000).approximateTrimming(),
                  Map.of("documentId", id.toString())));
    for (var future : futures) {
      try {
        future.get(10, java.util.concurrent.TimeUnit.SECONDS);
      } catch (Exception e) {
        throw new IllegalStateException("Queue publication incomplete", e);
      }
    }
  }

  public StreamMessage<String, String> receive(String consumer) {
    var old =
        connection
            .sync()
            .xautoclaim(
                STREAM,
                XAutoClaimArgs.Builder.xautoclaim(
                        Consumer.from(GROUP, consumer), Duration.ofMinutes(2), "0-0")
                    .count(1));
    if (!old.getMessages().isEmpty()) return old.getMessages().getFirst();
    var rows =
        connection
            .sync()
            .xreadgroup(
                Consumer.from(GROUP, consumer),
                XReadArgs.Builder.block(Duration.ofSeconds(5)).count(1),
                XReadArgs.StreamOffset.lastConsumed(STREAM));
    return rows == null || rows.isEmpty() ? null : rows.getFirst();
  }

  public void acknowledge(String id) {
    connection.sync().xack(STREAM, GROUP, id);
    connection.sync().xdel(STREAM, id);
  }

  public void dead(UUID document, String code) {
    connection
        .sync()
        .xadd(
            DEAD,
            XAddArgs.Builder.maxlen(10000).approximateTrimming(),
            Map.of("documentId", document.toString(), "code", code));
  }

  public void close() {
    connection.close();
    client.shutdown();
  }

  /** Explicit probe writes only isolated keys and removes them afterward. */
  public static void main(String[] args) {
    String prefix = "rescan-probe-" + UUID.randomUUID();
    try (var queue = new Queue(redisUrl(), prefix)) {
      try {
        UUID id = UUID.randomUUID();
        queue.publish(id);
        var message = queue.receive("first");
        if (message == null || !id.toString().equals(message.getBody().get("documentId")))
          throw new IllegalStateException("Stream read failed");
        var recovered =
            queue
                .connection
                .sync()
                .xautoclaim(
                    queue.STREAM,
                    XAutoClaimArgs.Builder.xautoclaim(
                            Consumer.from(GROUP, "second"), Duration.ZERO, "0-0")
                        .count(1));
        if (recovered.getMessages().size() != 1)
          throw new IllegalStateException("Auto-claim failed");
        queue.acknowledge(message.getId());
        queue.dead(id, "SYNTHETIC_PROBE");
        System.out.println("Redis Streams compatibility probe passed");
      } finally {
        queue.connection.sync().del(queue.STREAM, queue.DEAD);
      }
    }
  }
}
