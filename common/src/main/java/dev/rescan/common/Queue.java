package dev.rescan.common;

import io.lettuce.core.*;
import io.lettuce.core.api.StatefulRedisConnection;
import java.time.Duration;
import java.util.*;

public class Queue implements AutoCloseable {
  public static final String STREAM = "rescan:documents", GROUP = "workers", DEAD = "rescan:dead";
  private final RedisClient client;
  private final StatefulRedisConnection<String, String> connection;

  public Queue() {
    this(redisUrl());
  }

  private static String redisUrl() {
    String url = Settings.get("REDIS_URL", "");
    if (url.isEmpty()) {
      try (var secrets =
          software.amazon.awssdk.services.secretsmanager.SecretsManagerClient.create()) {
        url =
            secrets
                .getSecretValue(r -> r.secretId(Settings.require("REDIS_SECRET_ARN")))
                .secretString();
      }
    }
    return url;
  }

  public Queue(String url) {
    client = RedisClient.create(url);
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
}
