package dev.rescan.common;

import static org.junit.jupiter.api.Assertions.*;

import java.util.*;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.*;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.*;

@Testcontainers
class WorkStoreTest {
  @Test
  void managedConnectionsReadRotatedCredentials() throws Exception {
    store.jdbc.execute("CREATE ROLE rescan_rotation LOGIN PASSWORD 'first-test-password'");
    var secrets =
        org.mockito.Mockito.mock(
            software.amazon.awssdk.services.secretsmanager.SecretsManagerClient.class);
    try (var factory =
        org.mockito.Mockito.mockStatic(
            software.amazon.awssdk.services.secretsmanager.SecretsManagerClient.class)) {
      factory
          .when(software.amazon.awssdk.services.secretsmanager.SecretsManagerClient::create)
          .thenReturn(secrets);
      org.mockito.Mockito.when(
              secrets.getSecretValue(
                  org.mockito.ArgumentMatchers
                      .<java.util.function.Consumer<
                              software.amazon.awssdk.services.secretsmanager.model
                                  .GetSecretValueRequest.Builder>>
                          any()))
          .thenReturn(
              software.amazon.awssdk.services.secretsmanager.model.GetSecretValueResponse.builder()
                  .secretString(
                      "{\"username\":\"rescan_rotation\",\"password\":\"first-test-password\"}")
                  .build());
      var source =
          new ManagedSecretDataSource(
              DB.getJdbcUrl(), "test-secret", new com.fasterxml.jackson.databind.ObjectMapper());
      try (var connection = source.getConnection()) {
        assertTrue(connection.isValid(1));
      }
      store.jdbc.execute("ALTER ROLE rescan_rotation PASSWORD 'second-test-password'");
      org.mockito.Mockito.when(
              secrets.getSecretValue(
                  org.mockito.ArgumentMatchers
                      .<java.util.function.Consumer<
                              software.amazon.awssdk.services.secretsmanager.model
                                  .GetSecretValueRequest.Builder>>
                          any()))
          .thenReturn(
              software.amazon.awssdk.services.secretsmanager.model.GetSecretValueResponse.builder()
                  .secretString(
                      "{\"username\":\"rescan_rotation\",\"password\":\"second-test-password\"}")
                  .build());
      try (var connection = source.getConnection()) {
        assertTrue(connection.isValid(1));
      }
    }
  }

  @Container
  static final PostgreSQLContainer<?> DB =
      new PostgreSQLContainer<>(
          org.testcontainers.utility.DockerImageName.parse("docker.io/library/postgres:17.6")
              .asCompatibleSubstituteFor("postgres"));

  JobStore store;
  WorkStore work;
  UUID user, job, doc;

  @BeforeEach
  void setup() {
    var ds = new DriverManagerDataSource(DB.getJdbcUrl(), DB.getUsername(), DB.getPassword());
    Infrastructure.migrate(ds);
    store =
        new JobStore(
            new JdbcTemplate(ds), new TransactionTemplate(new DataSourceTransactionManager(ds)));
    work = new WorkStore(store);
    store.jdbc.update("TRUNCATE users,jobs,documents,outbox,processing_attempts CASCADE");
    user = store.user("test-owner");
    job = UUID.randomUUID();
    doc = UUID.randomUUID();
    store.jdbc.update(
        "INSERT INTO jobs(id,user_id,idempotency_key,manifest_hash,status,total) VALUES"
            + " (?,?,?,'hash','QUEUED',1)",
        job,
        user,
        UUID.randomUUID().toString());
    store.jdbc.update(
        "INSERT INTO documents(id,job_id,filename,size_bytes,source_key,source_version,status)"
            + " VALUES (?,?,'resume.txt',10,?,'v1','QUEUED')",
        doc,
        job,
        doc.toString());
  }

  @Test
  void duplicateDeliveryCannotDoubleComplete() {
    var first = work.claim(doc);
    assertNotNull(first);
    assertNull(work.claim(doc));
    assertTrue(work.complete(first, "output", () -> {}));
    assertFalse(work.complete(first, "duplicate", () -> fail("Duplicate must not write S3")));
    assertEquals("SUCCEEDED", store.detail(user, job).get("status"));
  }

  @Test
  void expiredLeaseIsRecoveredAndOldWorkerFenced() {
    var old = work.claim(doc);
    store.jdbc.update("UPDATE documents SET lease_until=now()-interval '1 second' WHERE id=?", doc);
    work.recover();
    var current = work.claim(doc);
    assertNotNull(current);
    assertEquals(2, current.attempt());
    assertFalse(work.complete(old, "old", () -> fail("Stale write")));
    assertTrue(work.complete(current, "new", () -> {}));
  }

  @Test
  void deletionPreventsPublication() {
    var claim = work.claim(doc);
    store.jdbc.update("UPDATE jobs SET status='DELETING' WHERE id=?", job);
    assertFalse(work.heartbeat(claim));
    assertFalse(work.complete(claim, "output", () -> fail("Deleted job must not write")));
    assertNull(work.claim(doc));
  }

  @Test
  void transientFailuresRetryButEventuallyFail() {
    for (int attempt = 1; attempt <= 3; attempt++) {
      var claim = work.claim(doc);
      assertEquals(attempt, claim.attempt());
      assertEquals(attempt == 3, work.fail(claim, "TEMPORARY", true));
      store.jdbc.update("UPDATE documents SET available_at=now() WHERE id=?", doc);
    }
    assertNull(work.claim(doc));
    assertEquals("FAILED", store.detail(user, job).get("status"));
  }

  @Test
  void uploadFailureDoesNotCommitSuccess() {
    var claim = work.claim(doc);
    assertThrows(
        IllegalStateException.class,
        () ->
            work.complete(
                claim,
                "output",
                () -> {
                  throw new IllegalStateException();
                }));
    assertEquals(
        "PROCESSING",
        store.jdbc.queryForObject("SELECT status FROM documents WHERE id=?", String.class, doc));
  }

  @Test
  void deniesDifferentOwner() {
    assertThrows(JobStore.MissingJob.class, () -> store.detail(store.user("other"), job));
  }
}
