package dev.rescan.worker;

import static org.junit.jupiter.api.Assertions.*;

import java.awt.image.BufferedImage;
import java.nio.file.*;
import java.util.*;
import java.util.UUID;
import javax.imageio.ImageIO;
import org.apache.pdfbox.pdmodel.*;
import org.apache.pdfbox.pdmodel.encryption.*;
import org.apache.pdfbox.pdmodel.font.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class DocumentParserTest {
  @TempDir Path dir;

  @Test
  void extractsTextAndStableEnvelope() throws Exception {
    Path file = dir.resolve("resume.txt");
    Files.writeString(file, "Jane Doe\nJava engineer\n");
    var result =
        new DocumentParser().parse(file, "resume.txt", UUID.randomUUID(), UUID.randomUUID());
    assertTrue(result.get("text").toString().contains("Java engineer"));
    assertEquals("TIKA", result.get("extractionMethod"));
    assertEquals(1, result.get("schemaVersion"));
  }

  @Test
  void rejectsEmptyContent() throws Exception {
    Path file = dir.resolve("empty.txt");
    Files.writeString(file, "  \n");
    assertThrows(
        DocumentParser.ParseFailure.class,
        () -> new DocumentParser().parse(file, "empty.txt", UUID.randomUUID(), UUID.randomUUID()));
  }

  @Test
  void mixedPdfUsesOcrOnlyOnBlankPageAndPreservesOrder() throws Exception {
    Path file = dir.resolve("mixed.pdf");
    try (var pdf = new PDDocument()) {
      for (int i = 0; i < 3; i++) {
        var page = new PDPage();
        pdf.addPage(page);
        if (i != 1)
          try (var content = new PDPageContentStream(pdf, page)) {
            content.beginText();
            content.setFont(new PDType1Font(Standard14Fonts.FontName.HELVETICA), 12);
            content.newLineAtOffset(20, 700);
            content.showText(i == 0 ? "First native page" : "Last native page");
            content.endText();
          }
      }
      pdf.save(file.toFile());
    }
    var parser =
        new DocumentParser() {
          @Override
          protected Map<String, Object> vision(Path source) throws Exception {
            try (var images = Files.list(source)) {
              assertEquals(
                  List.of("0002.png"), images.map(p -> p.getFileName().toString()).toList());
            }
            return Map.of(
                "pages",
                List.of(Map.of("page", 2, "text", "Scanned middle page", "method", "VIT")),
                "modelRevision",
                "test");
          }
        };
    var result = parser.parse(file, "mixed.pdf", UUID.randomUUID(), UUID.randomUUID());
    String text = result.get("text").toString();
    assertTrue(text.indexOf("First") < text.indexOf("Scanned"));
    assertTrue(text.indexOf("Scanned") < text.indexOf("Last"));
    assertEquals("TIKA_VIT", result.get("extractionMethod"));
  }

  @Test
  void pngRoutesToVisionDespiteWrongExtension() throws Exception {
    Path file = dir.resolve("input");
    ImageIO.write(new BufferedImage(100, 100, BufferedImage.TYPE_INT_RGB), "png", file.toFile());
    var parser =
        new DocumentParser() {
          @Override
          protected Map<String, Object> vision(Path source) {
            return Map.of(
                "text",
                "image text",
                "extractionMethod",
                "VIT",
                "ocrUsed",
                true,
                "warnings",
                List.of());
          }
        };
    var result = parser.parse(file, "resume.txt", UUID.randomUUID(), UUID.randomUUID());
    assertEquals("image/png", result.get("mediaType"));
    assertEquals("VIT", result.get("extractionMethod"));
  }

  @Test
  void encryptedPdfHasExplicitError() throws Exception {
    Path file = dir.resolve("encrypted.pdf");
    try (var pdf = new PDDocument()) {
      pdf.addPage(new PDPage());
      pdf.protect(new StandardProtectionPolicy("owner", "password", new AccessPermission()));
      pdf.save(file.toFile());
    }
    Exception error =
        assertThrows(
            Exception.class,
            () ->
                new DocumentParser()
                    .parse(file, "encrypted.pdf", UUID.randomUUID(), UUID.randomUUID()));
    assertEquals("ENCRYPTED_DOCUMENT", DocumentParser.classify(error));
  }
}
