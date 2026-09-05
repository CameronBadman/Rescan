package dev.rescan.controller;

import com.amazonaws.services.lambda.runtime.*;
import dev.rescan.common.*;
import dev.rescan.common.Queue;
import java.sql.Timestamp;
import java.time.*;
import java.util.*;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import software.amazon.awssdk.services.ecs.EcsClient;

public class Controller implements RequestHandler<Map<String, Object>, Map<String, Object>> {
  private static final System.Logger LOG = System.getLogger(Controller.class.getName());
  private final AnnotationConfigApplicationContext context =
      new AnnotationConfigApplicationContext(Infrastructure.class);
  private final JobStore store = context.getBean(JobStore.class);
  private final BlobStore blobs = new BlobStore();

  @Override
  public Map<String, Object> handleRequest(Map<String, Object> event, Context ignored) {
    UUID lock = UUID.randomUUID();
    try {
      if(store.jdbc.update("UPDATE controller_state SET lock_token=?,lock_until=unixepoch()+90 WHERE id=1 AND (lock_until IS NULL OR lock_until<unixepoch())",lock)!=1)
        return Map.of("skipped",true);
      try {
        try (var queue = new Queue()) {
          var work = new WorkStore(store);
          work.recover();
          work.dispatch(queue);
          cleanup();
          long outstanding =
              store.jdbc.queryForObject(
                  "SELECT count(*) FROM documents d JOIN jobs j ON j.id=d.job_id WHERE j.status IN"
                      + " ('QUEUED','PROCESSING') AND d.status IN"
                      + " ('QUEUED','RETRY_WAIT','PROCESSING')",
                  Long.class);
          if (outstanding > 0)
            store.jdbc.update("UPDATE controller_state SET empty_since=NULL WHERE id=1");
          else
            store.jdbc.update(
                "UPDATE controller_state SET empty_since=COALESCE(empty_since,unixepoch()) WHERE id=1");
          Timestamp since =
              store.jdbc.queryForObject(
                  "SELECT empty_since FROM controller_state WHERE id=1", Timestamp.class);
          long emptySeconds =
              since == null ? 0 : Duration.between(since.toInstant(), Instant.unixepoch()).getSeconds();
          int desired = desired(outstanding, emptySeconds);
          if (desired >= 0 && !Settings.get("ECS_CLUSTER", "").isBlank()) {
            try (var ecs = EcsClient.create()) {
              ecs.updateService(
                  r ->
                      r.cluster(Settings.require("ECS_CLUSTER"))
                          .service(Settings.require("ECS_WORKER_SERVICE"))
                          .desiredCount(desired));
            }
          }
          LOG.log(
              System.Logger.Level.INFO,
              "Controller outstanding={0} desired={1}",
              outstanding,
              desired);
          return Map.of("outstanding", outstanding, "desired", desired);
        }
      } finally {
        store.jdbc.update("UPDATE controller_state SET lock_token=NULL,lock_until=NULL WHERE id=1 AND lock_token=?",lock);
      }
    } catch (Exception e) {
      throw new IllegalStateException("Controller failed; capacity preserved", e);
    }
  }

  public static int desired(long outstanding, long emptySeconds) {
    if (outstanding < 0) throw new IllegalArgumentException("Negative workload");
    return outstanding > 0
        ? (int) Math.min(10, 1 + (outstanding - 1) / 25)
        : emptySeconds >= 300 ? 0 : -1;
  }

  private void cleanup() {
    store.jdbc.update(
        "UPDATE jobs SET status='DELETING',updated_at=unixepoch() WHERE status='UPLOADING' AND"
            + " created_at<unixepoch()-86400");
    var jobs =
        store.jdbc.queryForList(
            "SELECT id,updated_at FROM jobs WHERE status='DELETING' ORDER BY updated_at LIMIT 20");
    for (var job : jobs) {
      UUID id = (UUID) job.get("id");
      blobs.deletePrefix("jobs/" + id + "/");
      // A final sweep after upload URL expiry also removes late uploads.
      if (((Timestamp) job.get("updated_at"))
          .toInstant()
          .isBefore(Instant.unixepoch().minusSeconds(1200)))
        store.jdbc.update("DELETE FROM jobs WHERE id=? AND status='DELETING'", id);
    }
  }

  public static void main(String[] args) throws Exception {
    var controller = new Controller();
    if (args.length > 0 && args[0].equals("--migrate")) {
      Infrastructure.migrate(controller.context.getBean(TursoDb.class));
      return;
    }
    do {
      controller.handleRequest(Map.of(), null);
      if (args.length == 0) break;
      Thread.sleep(60000);
    } while (true);
  }
}
