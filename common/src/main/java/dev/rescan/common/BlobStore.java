package dev.rescan.common;

import java.net.URI;
import java.nio.file.Path;
import java.time.Duration;
import java.util.*;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.*;
import software.amazon.awssdk.services.s3.presigner.S3Presigner;

public class BlobStore implements AutoCloseable {
  public final S3Client client;
  private final S3Presigner signer;
  public final String bucket;

  public BlobStore() {
    bucket = Settings.require("S3_BUCKET");
    var config =
        S3Configuration.builder()
            .pathStyleAccessEnabled(!Settings.get("S3_ENDPOINT", "").isBlank())
            .build();
    var cb = S3Client.builder().serviceConfiguration(config);
    var sb = S3Presigner.builder().serviceConfiguration(config);
    if (!Settings.get("S3_ENDPOINT", "").isBlank()) {
      cb.endpointOverride(URI.create(Settings.require("S3_ENDPOINT")));
      sb.endpointOverride(URI.create(Settings.require("S3_ENDPOINT")));
    }
    client = cb.build();
    signer = sb.build();
  }

  public Map<String, Object> upload(UUID id, String key, long bytes) {
    var signed =
        signer.presignPutObject(
            p ->
                p.signatureDuration(Duration.ofMinutes(15))
                    .putObjectRequest(
                        r -> r.bucket(bucket).key(key).contentLength(bytes).ifNoneMatch("*")));
    var headers = new TreeMap<>(signed.signedHeaders());
    headers.remove("host");
    headers.remove("content-length");
    return Map.of(
        "documentId",
        id,
        "url",
        signed.url().toString(),
        "method",
        "PUT",
        "headers",
        headers,
        "expiresInSeconds",
        900);
  }

  public HeadObjectResponse head(String key) {
    return client.headObject(r -> r.bucket(bucket).key(key));
  }

  public void download(String key, String version, Path destination) {
    client.getObject(r -> r.bucket(bucket).key(key).versionId(version), destination);
  }

  public void putJson(String key, Path source) {
    client.putObject(
        r -> r.bucket(bucket).key(key).contentType("application/json"),
        RequestBody.fromFile(source));
  }

  public String resultUrl(String key) {
    return signer
        .presignGetObject(
            r ->
                r.signatureDuration(Duration.ofMinutes(5))
                    .getObjectRequest(
                        g -> g.bucket(bucket).key(key).responseContentType("application/json")))
        .url()
        .toString();
  }

  public void deletePrefix(String prefix) {
    // Always enumerate versions and markers, including objects created by stale attempts.
    for (var page : client.listObjectVersionsPaginator(r -> r.bucket(bucket).prefix(prefix))) {
      var ids = new ArrayList<ObjectIdentifier>();
      page.versions()
          .forEach(
              v ->
                  ids.add(
                      ObjectIdentifier.builder().key(v.key()).versionId(v.versionId()).build()));
      page.deleteMarkers()
          .forEach(
              v ->
                  ids.add(
                      ObjectIdentifier.builder().key(v.key()).versionId(v.versionId()).build()));
      for (int i = 0; i < ids.size(); i += 1000) {
        var batch = ids.subList(i, Math.min(i + 1000, ids.size()));
        var result = client.deleteObjects(r -> r.bucket(bucket).delete(d -> d.objects(batch)));
        if (!result.errors().isEmpty()) throw new IllegalStateException("S3 cleanup incomplete");
      }
    }
  }

  public void close() {
    signer.close();
    client.close();
  }
}
