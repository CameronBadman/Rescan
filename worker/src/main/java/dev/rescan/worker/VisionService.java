package dev.rescan.worker;

import dev.rescan.common.Settings;
import java.nio.file.*;
import java.util.*;

/** Warm OCR subprocess has no cloud credentials and listens only on a private Unix socket. */
final class VisionService implements AutoCloseable {
  private Process process;
  private final Path directory;
  final Path socket;

  VisionService() throws java.io.IOException {
    directory = Files.createTempDirectory("rescan-vision-");
    socket = directory.resolve("ocr.sock");
  }

  synchronized void ensure() throws Exception {
    if (process != null && process.isAlive() && Files.exists(socket)) return;
    stop();
    var builder =
        new ProcessBuilder(
            Settings.get("PYTHON_BIN", "python3"),
            Settings.get("VIT_SCRIPT", "worker/python/recognize.py"),
            "--serve",
            socket.toString());
    var env = builder.environment();
    var kept = new HashMap<String, String>();
    for (String key :
        List.of(
            "PATH",
            "LANG",
            "VIT_MODEL_DIR",
            "DETECTOR_MODEL_DIR",
            "MAX_PAGES",
            "MAX_PAGE_PIXELS",
            "OCR_THREADS")) if (env.containsKey(key)) kept.put(key, env.get(key));
    env.clear();
    env.putAll(kept);
    env.put("HF_HUB_OFFLINE", "1");
    env.put("TRANSFORMERS_OFFLINE", "1");
    process =
        builder
            .redirectOutput(ProcessBuilder.Redirect.DISCARD)
            .redirectError(ProcessBuilder.Redirect.DISCARD)
            .start();
    long deadline = System.nanoTime() + java.time.Duration.ofSeconds(180).toNanos();
    while (!Files.exists(socket)) {
      if (!process.isAlive() || System.nanoTime() > deadline) {
        stop();
        throw new IllegalStateException("OCR preload failed");
      }
      Thread.sleep(100);
    }
  }

  synchronized void stop() throws java.io.IOException {
    WorkerMain.kill(process);
    process = null;
    Files.deleteIfExists(socket);
  }

  public void close() throws java.io.IOException {
    stop();
    Files.deleteIfExists(directory);
  }
}
