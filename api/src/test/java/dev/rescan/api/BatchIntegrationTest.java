package dev.rescan.api;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.rescan.common.*;
import java.util.*;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.junit.jupiter.*;
import software.amazon.awssdk.services.s3.model.HeadObjectResponse;

@Testcontainers
class BatchIntegrationTest {
  @Container
  static final GenericContainer<?> DB =
      new GenericContainer<>(
              "ghcr.io/tursodatabase/libsql-server@sha256:134f3a465ade779e417b258d8e4fbfa8ca0a3212a2dbe83457b2fa104b75d54a")
          .withEnv("SQLD_HTTP_LISTEN_ADDR", "0.0.0.0:8080")
          .withExposedPorts(8080);

  JobStore store;
  BatchService batches;
  UUID user;
  BlobStore blobs;

  @BeforeEach
  void setup() {
    var db = new TursoDb("http://" + DB.getHost() + ":" + DB.getMappedPort(8080), () -> "");
    db.migrate();
    store = new JobStore(db);
    store.jdbc.update("DELETE FROM jobs");
    store.jdbc.update("DELETE FROM users");
    store.jdbc.update("DELETE FROM orphan_results");
    user = store.user("owner");
    blobs = mock(BlobStore.class);
    when(blobs.upload(any(), anyString(), anyLong()))
        .thenAnswer(
            inv ->
                Map.<String, Object>of("documentId", inv.getArgument(0), "url", "http://upload"));
    when(blobs.head(anyString()))
        .thenReturn(
            HeadObjectResponse.builder().contentLength(100L).versionId("immutable-v1").build());
    batches = new BatchService(store, blobs, new ObjectMapper());
  }

  @Test
  void persistsSubmitsAndPaginates4000WithoutDuplicateJobs() throws Exception {
    var files =
        new BatchService.Manifest(
            Collections.nCopies(4000, new BatchService.FileSpec("resume.txt", 100)));
    var response = batches.create(user, "batch-1", files);
    UUID job = (UUID) response.get("jobId");
    assertEquals(job, batches.create(user, "batch-1", files).get("jobId"));
    assertEquals(4000, store.jdbc.queryForObject("SELECT count(*) FROM documents", Integer.class));
    assertEquals("VERIFYING", batches.submit(user, job).get("status"));
    for (int i = 0; i < 40; i++) new Verification(store, blobs).chunk(job);
    assertEquals("QUEUED", store.owned(user, job).get("status"));
    batches.submit(user, job);
    assertEquals(4000, store.jdbc.queryForObject("SELECT count(*) FROM outbox", Integer.class));
    UUID cursor = UUID.fromString(JobsController.START);
    int count = 0;
    while (true) {
      var rows = store.documents(user, job, cursor, 100);
      if (rows.isEmpty()) break;
      count += rows.size();
      cursor = (UUID) rows.getLast().get("id");
    }
    assertEquals(4000, count);
  }

  @Test
  void rejectsReusingKeyForDifferentManifest() throws Exception {
    batches.create(
        user,
        "same",
        new BatchService.Manifest(List.of(new BatchService.FileSpec("one.txt", 100))));
    assertThrows(
        Errors.Conflict.class,
        () ->
            batches.create(
                user,
                "same",
                new BatchService.Manifest(List.of(new BatchService.FileSpec("two.txt", 100)))));
  }

  @Test
  void incompleteUploadNeverQueuesPartialBatch() throws Exception {
    UUID job =
        (UUID)
            batches
                .create(
                    user,
                    "missing",
                    new BatchService.Manifest(List.of(new BatchService.FileSpec("one.txt", 100))))
                .get("jobId");
    when(blobs.head(anyString()))
        .thenReturn(HeadObjectResponse.builder().contentLength(99L).versionId("v1").build());
    assertEquals("VERIFYING", batches.submit(user, job).get("status"));
    new Verification(store, blobs).chunk(job);
    assertEquals(0, store.jdbc.queryForObject("SELECT count(*) FROM outbox", Integer.class));
    assertEquals("UPLOADING", store.owned(user, job).get("status"));
    assertEquals(
        "INVALID_UPLOAD",
        store
            .documents(user, job, UUID.fromString(JobsController.START), 100)
            .getFirst()
            .get("error_code"));
    when(blobs.head(anyString()))
        .thenReturn(
            HeadObjectResponse.builder().contentLength(100L).versionId("fixed-version").build());
    batches.submit(user, job);
    new Verification(store, blobs).chunk(job);
    assertEquals("QUEUED", store.owned(user, job).get("status"));
    assertEquals(1, store.jdbc.queryForObject("SELECT count(*) FROM outbox", Integer.class));
  }

  @Test
  void activeVerifierLeaseSkipsExternalCallsAndResumesAfterExpiry() throws Exception {
    UUID job =
        (UUID)
            batches
                .create(
                    user,
                    "leased",
                    new BatchService.Manifest(List.of(new BatchService.FileSpec("one.txt", 100))))
                .get("jobId");
    batches.submit(user, job);
    store.jdbc.update(
        "UPDATE jobs SET verification_token=?,verification_until=unixepoch()+90 WHERE id=?",
        UUID.randomUUID(),
        job);
    var verification = new Verification(store, blobs);
    assertFalse(verification.chunk(job));
    verify(blobs, never()).head(anyString());
    store.jdbc.update("UPDATE jobs SET verification_until=unixepoch()-1 WHERE id=?", job);
    assertTrue(verification.chunk(job));
    assertEquals("QUEUED", store.owned(user, job).get("status"));
  }

  @Test
  void deletingDuringVerificationDoesNotQueue() throws Exception {
    UUID job =
        (UUID)
            batches
                .create(
                    user,
                    "delete-verification",
                    new BatchService.Manifest(List.of(new BatchService.FileSpec("one.txt", 100))))
                .get("jobId");
    batches.submit(user, job);
    when(blobs.head(anyString()))
        .thenAnswer(
            call -> {
              store.jdbc.update("UPDATE jobs SET status='DELETING' WHERE id=?", job);
              return HeadObjectResponse.builder().contentLength(100L).versionId("v1").build();
            });
    new Verification(store, blobs).chunk(job);
    assertEquals("DELETING", store.owned(user, job).get("status"));
    assertEquals(0, store.jdbc.queryForObject("SELECT count(*) FROM outbox", Integer.class));
  }
}
