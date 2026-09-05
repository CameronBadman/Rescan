package dev.rescan.common;

import java.util.*;
import software.amazon.awssdk.services.s3.model.S3Exception;

/** Resumable verification; S3 calls never execute inside a database transaction. */
public final class Verification {
  private final JobStore store;
  private final BlobStore blobs;

  public Verification(JobStore store, BlobStore blobs) {
    this.store = store;
    this.blobs = blobs;
  }

  public void chunk(UUID job) {
    UUID token = UUID.randomUUID();
    var claimed =
        store.jdbc.queryForList(
            "UPDATE jobs SET verification_token=?,verification_until=unixepoch()+90 WHERE id=? AND"
                + " status='VERIFYING' AND (verification_until IS NULL OR"
                + " verification_until<unixepoch()) RETURNING verification_generation",
            token,
            job);
    if (claimed.isEmpty()) return;
    long generation = ((Number) claimed.getFirst().get("verification_generation")).longValue();
    try {
      var docs =
          store.jdbc.queryForList(
              "SELECT id,source_key,size_bytes FROM documents WHERE job_id=? AND"
                  + " verified_generation<>? ORDER BY id LIMIT 100",
              job,
              generation);
      var checked = new ArrayList<Map<String, Object>>();
      try (var pool = java.util.concurrent.Executors.newFixedThreadPool(8)) {
        var futures =
            docs.stream()
                .map(
                    d ->
                        pool.submit(
                            () -> {
                              String version = "", error = "";
                              try {
                                var head = blobs.head((String) d.get("source_key"));
                                if (head.contentLength()
                                        != ((Number) d.get("size_bytes")).longValue()
                                    || head.versionId() == null
                                    || "null".equals(head.versionId())) error = "INVALID_UPLOAD";
                                else version = head.versionId();
                              } catch (S3Exception e) {
                                if (e.statusCode() == 404) error = "MISSING_UPLOAD";
                                else throw e;
                              }
                              return Map.<String, Object>of(
                                  "id", d.get("id").toString(), "version", version, "error", error);
                            }))
                .toList();
        for (var future : futures) checked.add(future.get());
      }
      String payload =
          new com.fasterxml.jackson.databind.ObjectMapper().writeValueAsString(checked);
      store.tx.executeWithoutResult(
          tx -> {
            if (store
                .jdbc
                .queryForList(
                    "SELECT id FROM jobs WHERE id=? AND status='VERIFYING' AND"
                        + " verification_generation=? AND verification_token=? AND"
                        + " verification_until>unixepoch()",
                    job,
                    generation,
                    token)
                .isEmpty()) return;
            store.jdbc.update(
                "UPDATE documents SET source_version=(SELECT"
                    + " nullif(json_extract(value,'$.version'),'') FROM json_each(?) WHERE"
                    + " json_extract(value,'$.id')=documents.id), error_code=(SELECT"
                    + " nullif(json_extract(value,'$.error'),'') FROM json_each(?) WHERE"
                    + " json_extract(value,'$.id')=documents.id),verified_generation=? WHERE"
                    + " job_id=? AND id IN (SELECT json_extract(value,'$.id') FROM json_each(?))",
                payload,
                payload,
                generation,
                job,
                payload);
            if (store.jdbc.queryForObject(
                    "SELECT count(*) FROM documents WHERE job_id=? AND verified_generation<>?",
                    Long.class,
                    job,
                    generation)
                == 0) {
              boolean invalid =
                  store.jdbc.queryForObject(
                          "SELECT count(*) FROM documents WHERE job_id=? AND error_code IS NOT"
                              + " NULL",
                          Long.class,
                          job)
                      > 0;
              if (!invalid) {
                store.jdbc.update(
                    "UPDATE documents SET status='QUEUED',updated_at=unixepoch() WHERE job_id=?",
                    job);
                store.jdbc.update(
                    "INSERT INTO outbox(document_id) SELECT id FROM documents WHERE job_id=? ON"
                        + " CONFLICT(document_id) DO NOTHING",
                    job);
              }
              store.jdbc.update(
                  "UPDATE jobs SET status=?,updated_at=unixepoch() WHERE id=?",
                  invalid ? "UPLOADING" : "QUEUED",
                  job);
            }
          });
    } catch (Exception e) {
      throw new IllegalStateException("Verification chunk failed", e);
    } finally {
      store.jdbc.update(
          "UPDATE jobs SET verification_token=NULL,verification_until=NULL WHERE id=? AND"
              + " verification_token=?",
          job,
          token);
    }
  }
}
