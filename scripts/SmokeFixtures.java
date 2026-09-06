import java.awt.*;
import java.awt.image.BufferedImage;
import java.nio.file.*;
import javax.imageio.ImageIO;
import org.apache.pdfbox.pdmodel.*;
import org.apache.pdfbox.pdmodel.font.*;
import org.apache.pdfbox.pdmodel.graphics.image.LosslessFactory;

/** Generate only synthetic fixtures in a new temporary directory. */
class SmokeFixtures {
  public static void main(String[] args) throws Exception {
    Path directory = Files.createTempDirectory("rescan-smoke-fixtures-");
    Files.writeString(
        directory.resolve("native.txt"), "Jane Doe\nSoftware Engineer\nJava and SQL\n");
    var image = new BufferedImage(1200, 500, BufferedImage.TYPE_INT_RGB);
    var graphics = image.createGraphics();
    graphics.setColor(Color.WHITE);
    graphics.fillRect(0, 0, 1200, 500);
    graphics.setColor(Color.BLACK);
    graphics.setFont(new Font("SansSerif", Font.PLAIN, 42));
    graphics.drawString("Jane Doe", 50, 90);
    graphics.drawString("Software Engineer", 50, 160);
    graphics.drawString("Java and SQL", 50, 230);
    graphics.dispose();
    for (String extension : new String[] {"png", "jpeg", "tiff"})
      ImageIO.write(image, extension, directory.resolve("image." + extension).toFile());
    try (var pdf = new PDDocument()) {
      for (int i = 0; i < 3; i++) {
        var page = new PDPage();
        pdf.addPage(page);
        try (var content = new PDPageContentStream(pdf, page)) {
          if (i == 1)
            content.drawImage(LosslessFactory.createFromImage(pdf, image), 20, 450, 570, 237);
          else {
            content.beginText();
            content.setFont(new PDType1Font(Standard14Fonts.FontName.HELVETICA), 16);
            content.newLineAtOffset(30, 700);
            content.showText(i == 0 ? "First native page" : "Last native page");
            content.endText();
          }
        }
      }
      pdf.save(directory.resolve("mixed.pdf").toFile());
    }
    System.out.println(directory);
  }
}
