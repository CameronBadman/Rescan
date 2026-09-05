package dev.rescan.controller;

import com.amazonaws.services.lambda.runtime.*;
import dev.rescan.common.*;
import dev.rescan.common.Queue;
import java.util.*;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;
import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.ecs.EcsClient;
import software.amazon.awssdk.services.lambda.LambdaClient;
import software.amazon.awssdk.services.lambda.model.InvocationType;

public class Controller implements RequestHandler<Map<String, Object>, Map<String, Object>> {
  private final AnnotationConfigApplicationContext context =
      new AnnotationConfigApplicationContext(Infrastructure.class);
  private final JobStore store = context.getBean(JobStore.class);
  private final BlobStore blobs = new BlobStore();

  public Map<String, Object> handleRequest(Map<String, Object> event, Context invocation) {
    if ("migrate".equals(event.get("action"))) {
      store.jdbc.migrate();
      return Map.of("migrated", true);
    }
    if ("warm".equals(event.get("action"))) {
      boolean enabled = Boolean.TRUE.equals(event.get("enabled"));
      store.jdbc.update(
          "UPDATE controller_state SET warm_until=CASE WHEN ? THEN unixepoch()+7200 ELSE NULL END"
              + " WHERE id=1",
          enabled);
    }
    UUID lock = UUID.randomUUID();
    if (store.jdbc.update(
            "UPDATE controller_state SET lock_token=?,lock_until=unixepoch()+90 WHERE id=1 AND"
                + " (lock_until IS NULL OR lock_until<unixepoch())",
            lock)
        != 1) return Map.of("skipped", true);
    long deadline = System.nanoTime() + java.time.Duration.ofSeconds(40).toNanos();
    try {
      var pending =
          store.jdbc.queryForList(
              "SELECT id FROM jobs WHERE status='VERIFYING' AND (verification_until IS NULL OR"
                  + " verification_until<unixepoch()) ORDER BY updated_at LIMIT 2");
      for (var job : pending) {
        String name = Settings.get("VERIFIER_FUNCTION", "");
        if (name.isEmpty()) new Verification(store, blobs).chunk((UUID) job.get("id"));
        else
          try (var lambda = LambdaClient.create()) {
            lambda.invoke(
                r ->
                    r.functionName(name)
                        .invocationType(InvocationType.EVENT)
                        .payload(SdkBytes.fromUtf8String("{\"jobId\":\"" + job.get("id") + "\"}")));
          }
      }
      var work = new WorkStore(store);
      work.recover();
      long outstanding =
          store.jdbc.queryForObject(
              "SELECT count(*) FROM documents d JOIN jobs j ON j.id=d.job_id WHERE j.status IN"
                  + " ('QUEUED','PROCESSING') AND d.status IN ('QUEUED','RETRY_WAIT','PROCESSING')",
              Long.class);
      if (outstanding > 0) {
        try (var queue = new Queue()) {
          work.dispatch(queue, deadline);
        }
        store.jdbc.update("UPDATE controller_state SET empty_since=NULL WHERE id=1");
      } else
        store.jdbc.update(
            "UPDATE controller_state SET empty_since=coalesce(empty_since,unixepoch()) WHERE id=1");
      var state =
          store
              .jdbc
              .queryForList(
                  "SELECT coalesce(unixepoch()-empty_since,0) AS idle,"
                      + " coalesce(warm_until>unixepoch(),0) AS warm FROM controller_state WHERE"
                      + " id=1")
              .getFirst();
      int count =
          desired(
              outstanding,
              ((Number) state.get("idle")).longValue(),
              ((Number) state.get("warm")).intValue() != 0);
      // A stale controller must not update ECS after its lease has expired.
      if (count >= 0
          && !Settings.get("ECS_CLUSTER", "").isBlank()
          && store.jdbc.queryForObject(
                  "SELECT count(*) FROM controller_state WHERE id=1 AND lock_token=? AND"
                      + " lock_until>unixepoch()",
                  Long.class,
                  lock)
              == 1) {
        try (var ecs = EcsClient.create()) {
          ecs.updateService(
              r ->
                  r.cluster(Settings.require("ECS_CLUSTER"))
                      .service(Settings.require("ECS_WORKER_SERVICE"))
                      .desiredCount(count));
        }
      }
      cleanup(deadline);
      return Map.of("outstanding", outstanding, "desired", count);
    } finally {
      store.jdbc.update(
          "UPDATE controller_state SET lock_token=NULL,lock_until=NULL WHERE id=1 AND lock_token=?",
          lock);
    }
  }

  public static int desired(long outstanding, long idle) {
    return desired(outstanding, idle, false);
  }

  public static int desired(long outstanding, long idle, boolean warm) {
    if (outstanding < 0) throw new IllegalArgumentException("Negative workload");
    if (outstanding > 0) return (int) Math.min(10, 1 + (outstanding - 1) / 25);
    return warm ? 1 : idle >= 60 ? 0 : -1;
  }

  private void cleanup(long deadline) {
    store.jdbc.update(
        "UPDATE jobs SET status='DELETING',updated_at=unixepoch() WHERE status='UPLOADING' AND"
            + " created_at<unixepoch()-86400");
    for (var job :
        store.jdbc.queryForList(
            "SELECT id FROM jobs WHERE status='DELETING' AND updated_at<unixepoch()-1200 ORDER BY"
                + " updated_at LIMIT 5")) {
      if (System.nanoTime() > deadline) return;
      UUID id = (UUID) job.get("id");
      blobs.deletePrefix("jobs/" + id + "/");
      store.jdbc.update("DELETE FROM jobs WHERE id=? AND status='DELETING'", id);
    }
    for (var row :
        store.jdbc.queryForList(
            "SELECT key FROM orphan_results WHERE created_at<unixepoch()-1200 LIMIT 20")) {
      if (System.nanoTime() > deadline) return;
      String key = (String) row.get("key");
      if (store.jdbc.queryForObject(
              "SELECT count(*) FROM documents WHERE result_key=?", Long.class, key)
          == 0) blobs.deletePrefix(key);
      store.jdbc.update("DELETE FROM orphan_results WHERE key=?", key);
    }
  }

  public static void main(String[] args) throws Exception {
    var controller = new Controller();
    if (args.length > 0 && args[0].equals("--migrate")) {
      controller.store.jdbc.migrate();
      return;
    }
    if (args.length > 0 && args[0].equals("--warm")) {
      controller.handleRequest(
          Map.of("action", "warm", "enabled", args.length < 2 || Boolean.parseBoolean(args[1])),
          null);
      return;
    }
    do {
      controller.handleRequest(Map.of(), null);
      if (args.length == 0) break;
      Thread.sleep(60000);
    } while (true);
  }
}
