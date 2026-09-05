package dev.rescan.common;

import java.util.*;

public class JobStore {
  public final TursoDb jdbc;
  public final TursoDb tx;

  public JobStore(TursoDb db) {
    this.jdbc = db;
    this.tx = db;
  }

  public UUID user(String subject) {
    jdbc.update(
        "INSERT INTO users(id,subject) VALUES (?,?) ON CONFLICT(subject) DO NOTHING",
        UUID.randomUUID(),
        subject);
    return jdbc.queryForObject("SELECT id FROM users WHERE subject=?", UUID.class, subject);
  }

  public Map<String, Object> owned(UUID user, UUID job) {
    var rows = jdbc.queryForList("SELECT * FROM jobs WHERE id=? AND user_id=?", job, user);
    if (rows.isEmpty()) throw new MissingJob();
    return rows.getFirst();
  }

  public List<Map<String, Object>> list(UUID user, UUID after, String status, int limit) {
    return jdbc.queryForList(
        "SELECT id,status,total,created_at,updated_at FROM jobs WHERE user_id=? AND id>? AND (?=''"
            + " OR status=?) ORDER BY id LIMIT ?",
        user,
        after,
        status,
        status,
        limit);
  }

  public Map<String, Object> detail(UUID user, UUID job) {
    var result = new LinkedHashMap<>(owned(user, job));
    result.remove("idempotency_key");
    result.remove("manifest_hash");
    result.remove("user_id");
    var counts = new TreeMap<String, Long>();
    for (String state :
        List.of("UPLOADING", "QUEUED", "PROCESSING", "RETRY_WAIT", "SUCCEEDED", "FAILED"))
      counts.put(state, 0L);
    for (var row :
        jdbc.queryForList(
            "SELECT status,count(*) AS n FROM documents WHERE job_id=? GROUP BY status", job))
      counts.put((String) row.get("status"), ((Number) row.get("n")).longValue());
    result.put("counts", counts);
    result.put(
        "uploaded",
        jdbc.queryForObject(
            "SELECT count(*) FROM documents WHERE job_id=? AND source_version IS NOT NULL",
            Long.class,
            job));
    return result;
  }

  public List<Map<String, Object>> documents(UUID user, UUID job, UUID after, int limit) {
    owned(user, job);
    return jdbc.queryForList(
        "SELECT id,filename,size_bytes,status,attempts,error_code FROM documents WHERE job_id=? AND"
            + " id>? ORDER BY id LIMIT ?",
        job,
        after,
        limit);
  }

  public static class MissingJob extends RuntimeException {}
}
