package dev.rescan.worker;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.rescan.common.Settings;
import java.io.*;
import java.nio.file.*;
import java.time.Instant;
import java.util.*;
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
    public static final int MAX_TEXT=10*1024*1024;
    public static final Set<String> IMAGES=Set.of("image/png","image/jpeg","image/tiff","image/bmp","image/x-ms-bmp");
    public record Failure(String code) {}
    public static class ParseFailure extends Exception {
        public final String code;
        public ParseFailure(String code) { super(code); this.code=code; }
    }
    public Map<String,Object> parse(Path source,String filename,UUID job,UUID doc) throws Exception {
        var metadata=new Metadata(); metadata.set(TikaCoreProperties.RESOURCE_NAME_KEY,filename);
        String type;
        try(var stream=TikaInputStream.get(source)) { type=new DefaultDetector().detect(stream,metadata).toString(); }
        if(type.equals("application/octet-stream") || type.contains("zip") || type.contains("x-executable")) throw new ParseFailure("UNSUPPORTED_FORMAT");
        Map<String,Object> extraction;
        if(IMAGES.contains(type)) extraction=image(source);
        else extraction=tika(source,metadata);
        String text=(String)extraction.get("text");
        if(text==null || text.isBlank()) throw new ParseFailure("EMPTY_TEXT");
        if(text.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>MAX_TEXT) throw new ParseFailure("TEXT_LIMIT");
        var fields=new TreeMap<String,List<String>>();
        for(String name:metadata.names()) fields.put(name,Arrays.asList(metadata.getValues(name)));
        var result=new LinkedHashMap<String,Object>();
        result.put("schemaVersion",1); result.put("jobId",job.toString()); result.put("documentId",doc.toString());
        result.put("filename",filename); result.put("mediaType",type); result.putAll(extraction);
        result.put("metadata",fields); result.put("processedAt",Instant.now().toString()); return result;
    }
    protected Map<String,Object> image(Path source) throws Exception { throw new ParseFailure("UNSUPPORTED_FORMAT"); }
    protected Map<String,Object> tika(Path source,Metadata metadata) throws Exception {
        var context=new ParseContext();
        var ocr=new TesseractOCRConfig(); ocr.setSkipOcr(true); context.set(TesseractOCRConfig.class,ocr);
        context.set(EmbeddedDocumentExtractor.class,new EmbeddedDocumentExtractor() {
            public boolean shouldParseEmbedded(Metadata m) { return false; }
            public void parseEmbedded(InputStream s,ContentHandler h,Metadata m,boolean html) {}
        });
        var handler=new BodyContentHandler(MAX_TEXT);
        try(var input=TikaInputStream.get(source)) { new AutoDetectParser().parse(input,handler,metadata,context); }
        return new LinkedHashMap<>(Map.of("text",handler.toString(),"extractionMethod","TIKA","ocrUsed",false,"warnings",List.of()));
    }
    public static void child(String[] args) throws Exception {
        Path output=Path.of(args[2]);
        var mapper=new ObjectMapper();
        try {
            var parsed=new DocumentParser().parse(Path.of(args[1]),args[3],UUID.fromString(args[4]),UUID.fromString(args[5]));
            mapper.writeValue(output.toFile(),parsed);
        } catch(Exception e) {
            String code=e instanceof ParseFailure f ? f.code : classify(e);
            mapper.writeValue(output.toFile(),new Failure(code)); System.exit(2);
        }
    }
    static String classify(Throwable error) {
        for(Throwable e=error;e!=null;e=e.getCause()) {
            String name=e.getClass().getSimpleName();
            if(name.contains("Encrypted") || name.contains("InvalidPassword")) return "ENCRYPTED_DOCUMENT";
            if(name.contains("WriteLimit")) return "TEXT_LIMIT";
        }
        return "CORRUPT_DOCUMENT";
    }
}
