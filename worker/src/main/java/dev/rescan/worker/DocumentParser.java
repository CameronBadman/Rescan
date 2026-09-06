package dev.rescan.worker;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.rescan.common.Settings;
import java.io.*;
import java.nio.file.*;
import java.time.Instant;
import java.util.*;
import javax.imageio.ImageIO;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.rendering.PDFRenderer;
import org.apache.pdfbox.text.PDFTextStripper;
import org.apache.tika.detect.DefaultDetector;
import org.apache.tika.extractor.EmbeddedDocumentExtractor;
import org.apache.tika.io.TikaInputStream;
import org.apache.tika.metadata.Metadata;
import org.apache.tika.metadata.TikaCoreProperties;
import org.apache.tika.parser.*;
import org.apache.tika.parser.ocr.TesseractOCRConfig;
import org.apache.tika.sax.BodyContentHandler;
import org.xml.sax.ContentHandler;

public class DocumentParser {
  public static final int MAX_TEXT = 10 * 1024 * 1024;
  public static final Set<String> IMAGES =
      Set.of("image/png", "image/jpeg", "image/tiff", "image/bmp", "image/x-ms-bmp");

  public record Failure(String code) {}

  public static class ParseFailure extends Exception {
    public final String code;

    public ParseFailure(String code) {
      super(code);
      this.code = code;
    }
  }

  public Map<String, Object> parse(Path source, String filename, UUID job, UUID doc)
      throws Exception {
    var metadata = new Metadata();
    metadata.set(TikaCoreProperties.RESOURCE_NAME_KEY, filename);
    String type;
    try (var stream = TikaInputStream.get(source)) {
      type = new DefaultDetector().detect(stream, metadata).toString();
    }
    if (type.equals("application/octet-stream")
        || Set.of(
                "application/zip",
                "application/gzip",
                "application/x-tar",
                "application/x-7z-compressed",
                "application/x-rar-compressed",
                "application/x-executable")
            .contains(type)) throw new ParseFailure("UNSUPPORTED_FORMAT");
    Map<String, Object> extraction;
    if (IMAGES.contains(type)) extraction = image(source);
    else if (type.equals("application/pdf")) extraction = pdf(source, metadata);
    else extraction = tika(source, metadata);
    String text = (String) extraction.get("text");
    if (text == null || text.isBlank()) throw new ParseFailure("EMPTY_TEXT");
    if (text.getBytes(java.nio.charset.StandardCharsets.UTF_8).length > MAX_TEXT)
      throw new ParseFailure("TEXT_LIMIT");
    var fields = new TreeMap<String, List<String>>();
    for (String name : metadata.names()) fields.put(name, Arrays.asList(metadata.getValues(name)));
    var result = new LinkedHashMap<String, Object>();
    result.put("schemaVersion", 1);
    result.put("jobId", job.toString());
    result.put("documentId", doc.toString());
    result.put("filename", filename);
    result.put("mediaType", type);
    result.putAll(extraction);
    result.put("metadata", fields);
    result.put("processedAt", Instant.now().toString());
    return result;
  }

  protected Map<String, Object> image(Path source) throws Exception {
    return vision(source);
  }

  protected Map<String, Object> vision(Path source) throws Exception {
    Path output = source.getParent().resolve("vision-" + UUID.randomUUID() + ".json");
    var process =
        new ProcessBuilder(
                Settings.get("PYTHON_BIN", "python3"),
                Settings.get("VIT_SCRIPT", "worker/python/recognize.py"),
                source.toString(),
                output.toString())
            .redirectOutput(ProcessBuilder.Redirect.DISCARD)
            .redirectError(ProcessBuilder.Redirect.DISCARD)
            .start();
    if (!process.waitFor(
        Settings.integer("DOCUMENT_TIMEOUT_SECONDS", 900), java.util.concurrent.TimeUnit.SECONDS)) {
      WorkerMain.kill(process);
      throw new ParseFailure("TIMEOUT");
    }
    if (process.exitValue() != 0) {
      String code =
          Files.exists(output)
              ? new ObjectMapper().readTree(output.toFile()).path("code").asText("OCR_FAILED")
              : "OCR_FAILED";
      throw new ParseFailure(code);
    }
    try {
      return new ObjectMapper()
          .readValue(
              output.toFile(),
              new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() {});
    } finally {
      Files.deleteIfExists(output);
    }
  }

  protected Map<String, Object> pdf(Path source, Metadata metadata) throws Exception {
    var pages = new TreeMap<Integer, Map<String, Object>>();
    Path images = Files.createTempDirectory(source.getParent(), "pages-");
    boolean ocr = false, nativeText = false;
    try (var document = Loader.loadPDF(source.toFile())) {
      if (document.isEncrypted()) throw new ParseFailure("ENCRYPTED_DOCUMENT");
      if (document.getNumberOfPages() > Settings.integer("MAX_PAGES", 50))
        throw new ParseFailure("PAGE_LIMIT");
      var stripper = new PDFTextStripper();
      stripper.setSortByPosition(true);
      var renderer = new PDFRenderer(document);
      for (int i = 0; i < document.getNumberOfPages(); i++) {
        stripper.setStartPage(i + 1);
        stripper.setEndPage(i + 1);
        if (!stripper.getText(document).isBlank()) {
          Path single = source.getParent().resolve("native-page.pdf");
          try (var pageDoc = new PDDocument()) {
            pageDoc.importPage(document.getPage(i));
            pageDoc.save(single.toFile());
          }
          var parsed = tika(single, metadata);
          Files.delete(single);
          pages.put(i + 1, Map.of("page", i + 1, "text", parsed.get("text"), "method", "TIKA"));
          nativeText = true;
        } else {
          var box = document.getPage(i).getCropBox();
          double pixels = box.getWidth() * box.getHeight() * Math.pow(200.0 / 72, 2);
          if (pixels > Settings.integer("MAX_PAGE_PIXELS", 12000000))
            throw new ParseFailure("IMAGE_LIMIT");
          ImageIO.write(
              renderer.renderImageWithDPI(i, 200),
              "PNG",
              images.resolve(String.format("%04d.png", i + 1)).toFile());
          ocr = true;
        }
      }
      Map<String, Object> recognized = ocr ? vision(images) : Map.of();
      if (ocr) {
        @SuppressWarnings("unchecked")
        var scanned = (List<Map<String, Object>>) recognized.get("pages");
        for (var page : scanned) pages.put(((Number) page.get("page")).intValue(), page);
      }
      var result = new LinkedHashMap<String, Object>();
      result.put(
          "text",
          String.join("\n\n", pages.values().stream().map(p -> p.get("text").toString()).toList()));
      result.put("pages", new ArrayList<>(pages.values()));
      result.put("ocrUsed", ocr);
      result.put("extractionMethod", ocr ? (nativeText ? "TIKA_VIT" : "VIT") : "TIKA");
      result.put("warnings", ocr ? recognized.getOrDefault("warnings", List.of()) : List.of());
      if (ocr) result.put("modelRevision", recognized.get("modelRevision"));
      return result;
    } finally {
      try (var paths = Files.walk(images)) {
        for (var path : paths.sorted(Comparator.reverseOrder()).toList())
          Files.deleteIfExists(path);
      }
    }
  }

  protected Map<String, Object> tika(Path source, Metadata metadata) throws Exception {
    var context = new ParseContext();
    var ocr = new TesseractOCRConfig();
    ocr.setSkipOcr(true);
    context.set(TesseractOCRConfig.class, ocr);
    context.set(
        EmbeddedDocumentExtractor.class,
        new EmbeddedDocumentExtractor() {
          public boolean shouldParseEmbedded(Metadata m) {
            return false;
          }

          public void parseEmbedded(InputStream s, ContentHandler h, Metadata m, boolean html) {}
        });
    var handler = new BodyContentHandler(MAX_TEXT);
    try (var input = TikaInputStream.get(source)) {
      new AutoDetectParser().parse(input, handler, metadata, context);
    }
    return new LinkedHashMap<>(
        Map.of(
            "text",
            handler.toString(),
            "extractionMethod",
            "TIKA",
            "ocrUsed",
            false,
            "warnings",
            List.of()));
  }

  public static void child(String[] args) throws Exception {
    // Parsers may interpret links but must never fetch remote document resources.
    java.net.URL.setURLStreamHandlerFactory(
        protocol -> {
          if (!Set.of("http", "https", "ftp").contains(protocol)) return null;
          return new java.net.URLStreamHandler() {
            @Override
            protected java.net.URLConnection openConnection(java.net.URL url) throws IOException {
              throw new IOException("External document resources are disabled");
            }
          };
        });
    Path output = Path.of(args[2]);
    var mapper = new ObjectMapper();
    try {
      var parsed =
          new DocumentParser()
              .parse(Path.of(args[1]), args[3], UUID.fromString(args[4]), UUID.fromString(args[5]));
      mapper.writeValue(output.toFile(), parsed);
    } catch (Exception e) {
      String code = e instanceof ParseFailure f ? f.code : classify(e);
      mapper.writeValue(output.toFile(), new Failure(code));
      System.exit(2);
    }
  }

  static String classify(Throwable error) {
    for (Throwable e = error; e != null; e = e.getCause()) {
      String name = e.getClass().getSimpleName();
      if (name.contains("Encrypted") || name.contains("InvalidPassword"))
        return "ENCRYPTED_DOCUMENT";
      if (name.contains("WriteLimit")) return "TEXT_LIMIT";
    }
    return "CORRUPT_DOCUMENT";
  }
}
