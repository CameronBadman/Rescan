package dev.rescan.common;

import static org.junit.jupiter.api.Assertions.*;

import java.util.*;
import org.junit.jupiter.api.*;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.junit.jupiter.*;

@Testcontainers
class WorkStoreTest {
  @Container
  static final GenericContainer<?> DB =
      new GenericContainer<>(
              "ghcr.io/tursodatabase/libsql-server@sha256:134f3a465ade779e417b258d8e4fbfa8ca0a3212a2dbe83457b2fa104b75d54a")
          .withEnv("SQLD_HTTP_LISTEN_ADDR", "0.0.0.0:8080")
          .withExposedPorts(8080);

  JobStore store;
  WorkStore work;
  UUID user, job, doc;

  @BeforeEach
  void setup() {
    var db = new TursoDb("http://" + DB.getHost() + ":" + DB.getMappedPort(8080), () -> "");
    db.migrate();
    store = new JobStore(db);
    store.jdbc.update("DELETE FROM jobs");
    store.jdbc.update("DELETE FROM users");
    store.jdbc.update("DELETE FROM orphan_results");
    work = new WorkStore(store);
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
    store.jdbc.update("UPDATE documents SET lease_until=unixepoch()-1 WHERE id=?", doc);
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
      store.jdbc.update("UPDATE documents SET available_at=unixepoch() WHERE id=?", doc);
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

  @Test
  void tenConcurrentConsumersHaveOnlyOneWinner() throws Exception {
    try (var pool = java.util.concurrent.Executors.newFixedThreadPool(10)) {
      var start = new java.util.concurrent.CountDownLatch(1);
      var futures = new ArrayList<java.util.concurrent.Future<WorkStore.Claim>>();
      for (int i = 0; i < 10; i++)
        futures.add(
            pool.submit(
                () -> {
                  start.await();
                  return work.claim(doc);
                }));
      start.countDown();
      int winners = 0;
      for (var future : futures) if (future.get() != null) winners++;
      assertEquals(1, winners);
      assertEquals(
          1,
          store.jdbc.queryForObject(
              "SELECT attempts FROM documents WHERE id=?", Integer.class, doc));
    }
  }

  @Test
  void deletionDuringS3UploadCannotPublish() {
    var claim = work.claim(doc);
    assertFalse(
        work.complete(
            claim,
            "orphan-key",
            () -> store.jdbc.update("UPDATE jobs SET status='DELETING' WHERE id=?", job)));
    assertEquals(
        1, store.jdbc.queryForObject("SELECT count(*) FROM orphan_results", Integer.class));
    assertNull(
        store.jdbc.queryForObject(
            "SELECT result_key FROM documents WHERE id=?", String.class, doc));
  }

  @Test
  void rollbackAndMigrationChecksumsAreEnforced() {
    assertThrows(
        IllegalStateException.class,
        () ->
            store.tx.executeWithoutResult(
                tx -> {
                  store.jdbc.update("UPDATE jobs SET status='FAILED' WHERE id=?", job);
                  throw new IllegalStateException("synthetic failure");
                }));
    assertEquals("QUEUED", store.owned(user, job).get("status"));
    store.jdbc.migrate();
    String checksum =
        store.jdbc.queryForObject(
            "SELECT checksum FROM schema_migrations WHERE version=1", String.class);
    try {
      store.jdbc.update("UPDATE schema_migrations SET checksum='invalid'");
      assertThrows(IllegalStateException.class, () -> store.jdbc.migrate());
    } finally {
      store.jdbc.update("UPDATE schema_migrations SET checksum=?", checksum);
    }
  }
}
