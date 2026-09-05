package dev.rescan.api;

import dev.rescan.common.*;
import java.util.*;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/jobs")
public class ResultsController {
    private final JobStore store; private final BlobStore blobs;
    public ResultsController(JobStore store,BlobStore blobs) { this.store=store; this.blobs=blobs; }
    @GetMapping("/{id}/documents/{documentId}/result") public Map<String,Object> result(@AuthenticationPrincipal Jwt jwt,@PathVariable UUID id,@PathVariable UUID documentId) {
        var job=store.owned(store.user(jwt.getSubject()),id);
        if("DELETING".equals(job.get("status"))) throw new JobStore.MissingJob();
        var rows=store.jdbc.queryForList("SELECT status,result_key FROM documents WHERE id=? AND job_id=?",documentId,id);
        if(rows.isEmpty()) throw new JobStore.MissingJob();
        if(!"SUCCEEDED".equals(rows.getFirst().get("status"))) throw new Errors.Conflict("Result is not ready");
        return Map.of("url",blobs.resultUrl((String)rows.getFirst().get("result_key")),"expiresInSeconds",300);
    }
}
