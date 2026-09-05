package dev.rescan.api;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.rescan.common.*;
import java.util.*;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.*;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.*;
import org.testcontainers.utility.DockerImageName;
import software.amazon.awssdk.services.s3.model.HeadObjectResponse;

@Testcontainers
class BatchIntegrationTest {
  @Container
  static final PostgreSQLContainer<?> DB =
      new PostgreSQLContainer<>(
          DockerImageName.parse("docker.io/library/postgres:17.6")
              .asCompatibleSubstituteFor("postgres"));

  JobStore store;
  BatchService batches;
  UUID user;
  BlobStore blobs;

  @BeforeEach
  void setup() {
    var ds = new DriverManagerDataSource(DB.getJdbcUrl(), DB.getUsername(), DB.getPassword());
    Infrastructure.migrate(ds);
    store =
        new JobStore(
            new JdbcTemplate(ds), new TransactionTemplate(new DataSourceTransactionManager(ds)));
    store.jdbc.update("TRUNCATE users,jobs,documents,outbox,processing_attempts CASCADE");
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
    assertEquals("QUEUED", batches.submit(user, job).get("status"));
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
    assertThrows(Errors.Conflict.class, () -> batches.submit(user, job));
    assertEquals(0, store.jdbc.queryForObject("SELECT count(*) FROM outbox", Integer.class));
    assertEquals("UPLOADING", store.owned(user, job).get("status"));
  }
}
