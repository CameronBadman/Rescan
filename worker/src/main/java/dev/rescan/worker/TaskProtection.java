package dev.rescan.worker;

import dev.rescan.common.Settings;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;

final class TaskProtection {
  private static final HttpClient CLIENT =
      HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

  static void set(boolean enabled) throws Exception {
    String agent = Settings.get("ECS_AGENT_URI", "");
    if (agent.isBlank()) return;
    var request =
        HttpRequest.newBuilder(URI.create(agent + "/task-protection/v1/state"))
            .timeout(Duration.ofSeconds(10))
            .header("Content-Type", "application/json")
            .PUT(
                HttpRequest.BodyPublishers.ofString(
                    "{\"ProtectionEnabled\":" + enabled + ",\"ExpiresInMinutes\":20}"))
            .build();
    if (CLIENT.send(request, HttpResponse.BodyHandlers.discarding()).statusCode() != 200)
      throw new IllegalStateException("Task protection unavailable");
  }
}
