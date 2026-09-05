package dev.rescan.common;

public final class Settings {
  private Settings() {}

  public static String get(String key, String fallback) {
    return System.getenv().getOrDefault(key, fallback);
  }

  public static String require(String key) {
    String value = System.getenv(key);
    if (value == null || value.isBlank())
      throw new IllegalStateException("Missing setting: " + key);
    return value;
  }

  public static int integer(String key, int fallback) {
    return Integer.parseInt(get(key, "" + fallback));
  }

  public static final int MAX_DOCUMENTS = 4000;

  public static long maxFileBytes() {
    return Long.parseLong(get("MAX_FILE_BYTES", "26214400"));
  }

  public static long maxBatchBytes() {
    return Long.parseLong(get("MAX_BATCH_BYTES", "5368709120"));
  }
}
