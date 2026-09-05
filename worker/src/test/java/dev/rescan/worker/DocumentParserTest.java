package dev.rescan.worker;

import java.nio.file.*;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import static org.junit.jupiter.api.Assertions.*;

class DocumentParserTest {
    @TempDir Path dir;
    @Test void extractsTextAndStableEnvelope() throws Exception {
        Path file=dir.resolve("resume.txt"); Files.writeString(file,"Jane Doe\nJava engineer\n");
        var result=new DocumentParser().parse(file,"resume.txt",UUID.randomUUID(),UUID.randomUUID());
        assertTrue(result.get("text").toString().contains("Java engineer"));
        assertEquals("TIKA",result.get("extractionMethod")); assertEquals(1,result.get("schemaVersion"));
    }
    @Test void rejectsEmptyContent() throws Exception {
        Path file=dir.resolve("empty.txt"); Files.writeString(file,"  \n");
        assertThrows(DocumentParser.ParseFailure.class,() -> new DocumentParser().parse(file,"empty.txt",UUID.randomUUID(),UUID.randomUUID()));
    }
}
