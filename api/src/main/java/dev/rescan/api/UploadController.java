package dev.rescan.api;

import dev.rescan.common.*;
import java.util.*;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.*;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/jobs")
public class UploadController {
  private final BatchService batches;
  private final JobStore store;

  public UploadController(BatchService batches, JobStore store) {
    this.batches = batches;
    this.store = store;
  }

  @PostMapping
  public ResponseEntity<?> create(
      @AuthenticationPrincipal Jwt jwt,
      @RequestHeader("Idempotency-Key") String key,
      @RequestBody BatchService.Manifest manifest)
      throws Exception {
    return ResponseEntity.status(201)
        .body(batches.create(store.user(jwt.getSubject()), key, manifest));
  }

  @PostMapping("/{id}/upload-urls")
  public Map<String, Object> uploads(
      @AuthenticationPrincipal Jwt jwt,
      @PathVariable UUID id,
      @RequestParam(defaultValue = JobsController.START) UUID after,
      @RequestParam(defaultValue = "100") int limit) {
    return batches.uploads(store.user(jwt.getSubject()), id, after, limit);
  }

  @PostMapping("/{id}/submit")
  public ResponseEntity<?> submit(@AuthenticationPrincipal Jwt jwt, @PathVariable UUID id) {
    return ResponseEntity.accepted().body(batches.submit(store.user(jwt.getSubject()), id));
  }

  @Configuration
  static class StorageConfig {
    @Bean
    BlobStore blobs() {
      return new BlobStore();
    }
  }
}
