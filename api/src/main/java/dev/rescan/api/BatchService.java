package dev.rescan.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.rescan.common.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import org.springframework.stereotype.Service;

@Service
public class BatchService {
  public record FileSpec(String filename, long sizeBytes) {}

  public record Manifest(List<FileSpec> files) {}

  private final JobStore store;
  private final BlobStore blobs;
  private final ObjectMapper json;

  public BatchService(JobStore store, BlobStore blobs, ObjectMapper json) {
    this.store = store;
    this.blobs = blobs;
    this.json = json;
  }

  public static void validate(Manifest manifest) {
    if (manifest == null
        || manifest.files() == null
        || manifest.files().isEmpty()
        || manifest.files().size() > Settings.MAX_DOCUMENTS)
      throw new IllegalArgumentException("Batch must contain 1–4000 files");
    long total = 0;
    for (var file : manifest.files()) {
      if (file == null
          || file.filename() == null
          || file.filename().isBlank()
          || file.filename().length() > 255
          || file.filename().chars().anyMatch(Character::isISOControl))
        throw new IllegalArgumentException("Invalid filename");
      if (file.sizeBytes() < 1 || file.sizeBytes() > Settings.maxFileBytes())
        throw new IllegalArgumentException("File size exceeds limit");
      total = Math.addExact(total, file.sizeBytes());
    }
    if (total > Settings.maxBatchBytes())
      throw new IllegalArgumentException("Batch size exceeds limit");
  }

  public Map<String, Object> create(UUID user, String key, Manifest manifest) throws Exception {
    validate(manifest);
    if (key == null || key.isBlank() || key.length() > 128)
      throw new IllegalArgumentException("Idempotency-Key is required, maximum 128 characters");
    String hash =
        HexFormat.of()
            .formatHex(
                MessageDigest.getInstance("SHA-256")
                    .digest(json.writeValueAsString(manifest).getBytes(StandardCharsets.UTF_8)));
    UUID job =
        store.tx.execute(
            status -> {
              // Serialize create requests for this owner, including requests with the same key.
              store.jdbc.queryForObject("SELECT id FROM users WHERE id=?", UUID.class, user);
              var existing =
                  store.jdbc.queryForList(
                      "SELECT id,manifest_hash FROM jobs WHERE user_id=? AND idempotency_key=?",
                      user,
                      key);
              if (!existing.isEmpty()) {
                if (!hash.equals(existing.getFirst().get("manifest_hash")))
                  throw new Errors.Conflict(
                      "Idempotency key already used for a different manifest");
                return (UUID) existing.getFirst().get("id");
              }
              UUID id = UUID.randomUUID();
              store.jdbc.update(
                  "INSERT INTO jobs(id,user_id,idempotency_key,manifest_hash,total) VALUES"
                      + " (?,?,?,?,?)",
                  id,
                  user,
                  key,
                  hash,
                  manifest.files().size());
              var args = new ArrayList<Object[]>();
              int index = 0;
              for (var file : manifest.files()) {
                UUID doc = UUID.randomUUID();
                args.add(
                    new Object[] {
                      doc,
                      id,
                      file.filename(),
                      index++,
                      file.sizeBytes(),
                      "jobs/" + id + "/originals/" + doc
                    });
              }
              store.jdbc.batchUpdate(
                  "INSERT INTO documents(id,job_id,filename,file_index,size_bytes,source_key)"
                      + " VALUES (?,?,?,?,?,?)",
                  args);
              return id;
            });
    var result = new LinkedHashMap<String, Object>();
    result.put("jobId", job);
    result.put("total", manifest.files().size());
    if ("UPLOADING".equals(store.owned(user, job).get("status")))
      result.putAll(uploads(user, job, UUID.fromString(JobsController.START), 100));
    return result;
  }

  public Map<String, Object> uploads(UUID user, UUID job, UUID after, int limit) {
    JobsController.pageSize(limit);
    if (!"UPLOADING".equals(store.owned(user, job).get("status")))
      throw new Errors.Conflict("Job is already submitted");
    var rows =
        store.jdbc.queryForList(
            "SELECT id,source_key,size_bytes,filename,file_index FROM documents WHERE job_id=? AND"
                + " id>? ORDER BY id LIMIT ?",
            job,
            after,
            limit + 1);
    boolean more = rows.size() > limit;
    var items =
        (more ? rows.subList(0, limit) : rows)
            .stream()
                .map(
                    r -> {
                      var item =
                          new LinkedHashMap<>(
                              blobs.upload(
                                  (UUID) r.get("id"),
                                  (String) r.get("source_key"),
                                  ((Number) r.get("size_bytes")).longValue()));
                      item.put("filename", r.get("filename"));
                      item.put("fileIndex", r.get("file_index"));
                      return item;
                    })
                .toList();
    var response = new LinkedHashMap<String, Object>();
    response.put("uploads", items);
    response.put("nextCursor", more ? rows.get(limit - 1).get("id") : null);
    return response;
  }

  public Map<String, Object> submit(UUID user, UUID job) {
    return store.tx.execute(
        tx -> {
          var current = store.owned(user, job);
          if ("DELETING".equals(current.get("status")))
            throw new Errors.Conflict("Job is being deleted");
          if ("UPLOADING".equals(current.get("status"))) {
            store.jdbc.update(
                "UPDATE jobs SET"
                    + " status='VERIFYING',verification_generation=verification_generation+1,verification_token=NULL,verification_until=NULL,updated_at=unixepoch()"
                    + " WHERE id=?",
                job);
            return Map.of("jobId", job, "status", "VERIFYING");
          }
          return Map.of("jobId", job, "status", current.get("status"));
        });
  }
}
