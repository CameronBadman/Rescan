package dev.rescan.api;

import dev.rescan.common.JobStore;
import java.util.*;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/jobs")
public class JobsController {
    private final JobStore store;
    public JobsController(JobStore store) { this.store = store; }
    static final String START = "00000000-0000-0000-0000-000000000000";
    static int pageSize(int limit) { if(limit < 1 || limit > 100) throw new IllegalArgumentException("limit must be 1–100"); return limit; }
    static Map<String,Object> page(List<Map<String,Object>> rows, int limit) {
        boolean more = rows.size() > limit;
        var items = more ? rows.subList(0,limit) : rows;
        var result = new LinkedHashMap<String,Object>(); result.put("items",items);
        result.put("nextCursor",more ? items.getLast().get("id") : null); return result;
    }
    @GetMapping public Map<String,Object> list(@AuthenticationPrincipal Jwt jwt, @RequestParam(defaultValue=START) UUID after,
            @RequestParam(defaultValue="") String status, @RequestParam(defaultValue="50") int limit) {
        pageSize(limit);
        if (!status.isEmpty() && !Set.of("UPLOADING","QUEUED","PROCESSING","SUCCEEDED","PARTIAL_SUCCESS","FAILED","DELETING").contains(status)) throw new IllegalArgumentException("Invalid status");
        return page(store.list(store.user(jwt.getSubject()),after,status,limit+1),limit);
    }
    @GetMapping("/{id}") public Map<String,Object> detail(@AuthenticationPrincipal Jwt jwt, @PathVariable UUID id) {
        return store.detail(store.user(jwt.getSubject()),id);
    }
    @GetMapping("/{id}/documents") public Map<String,Object> documents(@AuthenticationPrincipal Jwt jwt, @PathVariable UUID id,
            @RequestParam(defaultValue=START) UUID after, @RequestParam(defaultValue="50") int limit) {
        pageSize(limit); return page(store.documents(store.user(jwt.getSubject()),id,after,limit+1),limit);
    }
}
