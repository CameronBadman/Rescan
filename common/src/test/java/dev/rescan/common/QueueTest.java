package dev.rescan.common;

import static org.junit.jupiter.api.Assertions.*;

import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.junit.jupiter.*;

@Testcontainers
class QueueTest {
  @Container
  static final GenericContainer<?> REDIS =
      new GenericContainer<>("docker.io/library/redis:7.2.10").withExposedPorts(6379);

  @Test
  void publishesAndAcknowledges() {
    String url = "redis://" + REDIS.getHost() + ":" + REDIS.getMappedPort(6379);
    try (var queue = new Queue(url);
        var second = new Queue(url)) {
      UUID id = UUID.randomUUID();
      queue.publish(id);
      var message = second.receive("test-worker");
      assertEquals(id.toString(), message.getBody().get("documentId"));
      second.acknowledge(message.getId());
    }
  }
}
