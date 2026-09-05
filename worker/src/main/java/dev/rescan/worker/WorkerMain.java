package dev.rescan.worker;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.rescan.common.*;
import dev.rescan.common.Queue;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;

public class WorkerMain {
  private static final System.Logger LOG = System.getLogger(WorkerMain.class.getName());
  private static final AtomicBoolean RUNNING = new AtomicBoolean(true);
  private static final AtomicReference<Process> CHILD = new AtomicReference<>();
  private static VisionService vision;

  public static void main(String[] args) throws Exception {
    if (args.length > 0 && args[0].equals("--parse")) {
      DocumentParser.child(args);
      return;
    }
    Runtime.getRuntime()
        .addShutdownHook(
            new Thread(
                () -> {
                  RUNNING.set(false);
                  kill(CHILD.get());
                }));
    try (var service = new VisionService();
        var context = new AnnotationConfigApplicationContext(Infrastructure.class);
        var blobs = new BlobStore();
        var queue = new Queue()) {
      var work = new WorkStore(context.getBean(JobStore.class));
      vision = service;
      if (Boolean.parseBoolean(Settings.get("OCR_PRELOAD", "true"))) vision.ensure();
      Files.writeString(Path.of("/tmp/rescan-worker-ready"),"ready");
      LOG.log(System.Logger.Level.INFO,"Worker ready; OCR preload complete");
      String consumer = UUID.randomUUID().toString();
      while (RUNNING.get()) {
        try {
          var message = queue.receive(consumer);
          if (message == null) continue;
          var claim = work.claim(UUID.fromString(message.getBody().get("documentId")));
          if (claim != null) process(claim, work, blobs, queue);
          queue.acknowledge(message.getId());
        } catch (Exception e) {
          LOG.log(
              System.Logger.Level.WARNING,
              "Worker iteration failed: {0}",
              e.getClass().getSimpleName());
          Thread.sleep(1000);
        }
      }
    }
  }

  private static void process(WorkStore.Claim c, WorkStore work, BlobStore blobs, Queue queue)
      throws Exception {
    Path temp = Files.createTempDirectory("rescan-document-");
    try (var heartbeat = Executors.newSingleThreadScheduledExecutor()) {
      TaskProtection.set(true);
      var alive = new AtomicBoolean(true);
      heartbeat.scheduleAtFixedRate(
          () -> {
            try {
              if (!work.heartbeat(c)) {
                alive.set(false);
                kill(CHILD.get());
                vision.stop();
              }
            } catch (Exception e) {
              alive.set(false);
              kill(CHILD.get());
              try { vision.stop(); } catch(java.io.IOException ignored) { }
            }
          },
          20,
          20,
          TimeUnit.SECONDS);
      Path input = temp.resolve("input"), output = temp.resolve("output.json");
      blobs.download(c.sourceKey(), c.sourceVersion(), input);
      if (Files.size(input) != c.bytes() || Files.size(input) > Settings.maxFileBytes())
        throw new DocumentParser.ParseFailure("SIZE_MISMATCH");
      var builder =
          new ProcessBuilder(
              Path.of(System.getProperty("java.home"), "bin", "java").toString(),
              "-Xmx2g",
              "-Djava.io.tmpdir=" + temp,
              "-Djavax.xml.accessExternalDTD=",
              "-Djavax.xml.accessExternalSchema=",
              "-Djavax.xml.accessExternalStylesheet=",
              "-cp",
              System.getProperty("java.class.path"),
              WorkerMain.class.getName(),
              "--parse",
              input.toString(),
              output.toString(),
              c.filename(),
              c.jobId().toString(),
              c.id().toString());
      // The parser does not need credentials, service discovery, or application secrets.
      var env = builder.environment();
      var kept = new HashMap<String, String>();
      for (String key :
          List.of(
              "PATH",
              "LANG",
              "PYTHON_BIN",
              "VIT_SCRIPT",
              "VIT_MODEL_DIR",
              "DETECTOR_MODEL_DIR",
              "MAX_PAGES",
              "MAX_PAGE_PIXELS")) if (env.containsKey(key)) kept.put(key, env.get(key));
      env.clear();
      env.putAll(kept);
      env.put("HF_HUB_OFFLINE", "1");
      env.put("TRANSFORMERS_OFFLINE", "1");
      if (Boolean.parseBoolean(Settings.get("OCR_PRELOAD", "true"))) {
        vision.ensure();
        env.put("VIT_SOCKET", vision.socket.toString());
      }
      builder
          .redirectOutput(ProcessBuilder.Redirect.DISCARD)
          .redirectError(ProcessBuilder.Redirect.DISCARD);
      Process child = builder.start();
      CHILD.set(child);
      if (!child.waitFor(Settings.integer("DOCUMENT_TIMEOUT_SECONDS", 900), TimeUnit.SECONDS)) {
        kill(child);
        vision.stop();
        throw new DocumentParser.ParseFailure("TIMEOUT");
      }
      CHILD.compareAndSet(child, null);
      if (!alive.get() || !RUNNING.get()) return;
      if (child.exitValue() != 0) {
        String code =
            Files.exists(output)
                ? new ObjectMapper().readTree(output.toFile()).path("code").asText("PARSER_FAILED")
                : "PARSER_FAILED";
        throw new DocumentParser.ParseFailure(code);
      }
      String key = "jobs/" + c.jobId() + "/results/" + c.id() + "/" + c.token() + ".json";
      work.complete(c, key, () -> blobs.putJson(key, output));
      LOG.log(System.Logger.Level.INFO, "Document completed: {0}", c.id());
    } catch (DocumentParser.ParseFailure e) {
      boolean terminal =
          work.fail(c, e.code, Set.of("TIMEOUT", "PARSER_FAILED", "OCR_FAILED").contains(e.code));
      if (terminal) queue.dead(c.id(), e.code);
    } catch (Exception e) {
      boolean terminal = work.fail(c, "PROCESSING_UNAVAILABLE", true);
      if (terminal) queue.dead(c.id(), "PROCESSING_UNAVAILABLE");
    } finally {
      kill(CHILD.getAndSet(null));
      try {
        TaskProtection.set(false);
      } catch (Exception ignored) {
      }
      try (var paths = Files.walk(temp)) {
        for (Path path : paths.sorted(Comparator.reverseOrder()).toList())
          Files.deleteIfExists(path);
      }
    }
  }

  static void kill(Process process) {
    if (process == null) return;
    process.descendants().forEach(ProcessHandle::destroyForcibly);
    process.destroyForcibly();
    try {
      process.waitFor(10, TimeUnit.SECONDS);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
    }
  }
}
