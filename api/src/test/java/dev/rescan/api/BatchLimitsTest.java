package dev.rescan.api;

import static org.junit.jupiter.api.Assertions.*;

import java.util.*;
import org.junit.jupiter.api.Test;

class BatchLimitsTest {
  private BatchService.Manifest manifest(int count, long bytes) {
    return new BatchService.Manifest(
        Collections.nCopies(count, new BatchService.FileSpec("resume.pdf", bytes)));
  }

  @Test
  void accepts4000() {
    assertDoesNotThrow(() -> BatchService.validate(manifest(4000, 1000)));
  }

  @Test
  void rejects4001() {
    assertThrows(IllegalArgumentException.class, () -> BatchService.validate(manifest(4001, 1000)));
  }

  @Test
  void rejectsEmptyBatch() {
    assertThrows(IllegalArgumentException.class, () -> BatchService.validate(manifest(0, 1)));
  }

  @Test
  void rejectsOversizedFile() {
    assertThrows(
        IllegalArgumentException.class, () -> BatchService.validate(manifest(1, 26214401)));
  }

  @Test
  void rejectsAggregateLimit() {
    assertThrows(
        IllegalArgumentException.class, () -> BatchService.validate(manifest(4000, 2000000)));
  }
}
