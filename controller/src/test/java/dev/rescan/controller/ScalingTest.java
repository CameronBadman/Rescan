package dev.rescan.controller;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.Test;

class ScalingTest {
  @Test
  void wakesFromZero() {
    assertEquals(1, Controller.desired(1, 0));
  }

  @Test
  void respectsCeiling() {
    assertEquals(10, Controller.desired(4000, 0));
  }

  @Test
  void countsOutstandingRetries() {
    assertEquals(2, Controller.desired(26, 0));
  }

  @Test
  void waitsForIdleWindow() {
    assertEquals(-1, Controller.desired(0, 59));
    assertEquals(0, Controller.desired(0, 60));
  }

  @Test
  void warmSessionOnlyChangesMinimum() {
    assertEquals(1, Controller.desired(0, 600, true));
    assertEquals(10, Controller.desired(4000, 600, true));
    assertEquals(0, Controller.desired(0, 600, false));
  }
}
