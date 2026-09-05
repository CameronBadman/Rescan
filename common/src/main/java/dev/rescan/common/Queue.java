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
    return Secrets.get("REDIS_URL","REDIS_SECRET_ARN");
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

  public void publishMany(List<UUID> documents) {
    var futures=new ArrayList<io.lettuce.core.RedisFuture<String>>();
    for(UUID id:documents) futures.add(connection.async().xadd(STREAM,XAddArgs.Builder.maxlen(100000).approximateTrimming(),Map.of("documentId",id.toString())));
    for(var future:futures) {
      try { future.get(10,java.util.concurrent.TimeUnit.SECONDS); }
      catch(Exception e) { throw new IllegalStateException("Queue publication incomplete",e); }
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
}
