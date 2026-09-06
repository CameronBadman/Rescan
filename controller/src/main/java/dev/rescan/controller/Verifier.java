package dev.rescan.controller;

import com.amazonaws.services.lambda.runtime.*;
import dev.rescan.common.*;
import java.util.*;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;

public final class Verifier implements RequestHandler<Map<String, Object>, Map<String, Object>> {
  private final AnnotationConfigApplicationContext context =
      new AnnotationConfigApplicationContext(Infrastructure.class);
  private final BlobStore blobs = new BlobStore();

  public Map<String, Object> handleRequest(Map<String, Object> event, Context invocation) {
    UUID job = UUID.fromString(event.get("jobId").toString());
    var store = context.getBean(JobStore.class);
    var verification = new Verification(store, blobs);
    do {
      if (!verification.chunk(job)) break;
      var rows = store.jdbc.queryForList("SELECT status FROM jobs WHERE id=?", job);
      if (rows.isEmpty() || !"VERIFYING".equals(rows.getFirst().get("status"))) break;
    } while (invocation != null && invocation.getRemainingTimeInMillis() > 30000);
    return Map.of("jobId", job.toString());
  }
}
