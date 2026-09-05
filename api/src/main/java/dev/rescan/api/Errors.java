package dev.rescan.api;

import dev.rescan.common.JobStore;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestControllerAdvice
public class Errors {
    @ExceptionHandler(JobStore.MissingJob.class) ResponseEntity<?> missing() { return ResponseEntity.status(404).body(Map.of("code","NOT_FOUND")); }
    @ExceptionHandler(IllegalArgumentException.class) ResponseEntity<?> invalid(IllegalArgumentException error) { return ResponseEntity.badRequest().body(Map.of("code","INVALID_REQUEST","message",error.getMessage())); }
    @ExceptionHandler(Conflict.class) ResponseEntity<?> conflict(Conflict error) { return ResponseEntity.status(409).body(Map.of("code","CONFLICT","message",error.getMessage())); }
    public static class Conflict extends RuntimeException { public Conflict(String message) { super(message); } }
}
