package dev.rescan.common;

import java.util.*;

public class WorkStore {
    public record Claim(UUID id,UUID jobId,UUID token,String filename,String sourceKey,String sourceVersion,long bytes,int attempt) {}
    private final JobStore store;
    public WorkStore(JobStore store) { this.store=store; }

    public Claim claim(UUID document) {
        return store.tx.execute(tx -> {
            var refs=store.jdbc.queryForList("SELECT job_id FROM documents WHERE id=?",document);
            if(refs.isEmpty()) return null;
            UUID job=(UUID)refs.getFirst().get("job_id");
            var states=store.jdbc.queryForList("SELECT status FROM jobs WHERE id=? FOR UPDATE",job);
            if(states.isEmpty() || !Set.of("QUEUED","PROCESSING").contains(states.getFirst().get("status"))) return null;
            UUID token=UUID.randomUUID();
            var rows=store.jdbc.queryForList("""
                UPDATE documents SET status='PROCESSING', attempts=attempts+1, token=?,
                  lease_until=now()+interval '2 minutes',updated_at=now()
                WHERE id=? AND status IN ('QUEUED','RETRY_WAIT') AND available_at<=now() AND attempts<3
                RETURNING *
                """,token,document);
            if(rows.isEmpty()) return null;
            var d=rows.getFirst();
            store.jdbc.update("UPDATE jobs SET status='PROCESSING',updated_at=now() WHERE id=?",job);
            store.jdbc.update("INSERT INTO processing_attempts(token,document_id,attempt) VALUES (?,?,?)",token,document,d.get("attempts"));
            return new Claim(document,job,token,(String)d.get("filename"),(String)d.get("source_key"),(String)d.get("source_version"),((Number)d.get("size_bytes")).longValue(),((Number)d.get("attempts")).intValue());
        });
    }
    public boolean heartbeat(Claim c) {
        return store.jdbc.update("UPDATE documents SET lease_until=now()+interval '2 minutes' WHERE id=? AND token=? AND status='PROCESSING' AND lease_until>now() AND EXISTS(SELECT 1 FROM jobs WHERE id=documents.job_id AND status<>'DELETING')",c.id(),c.token())==1;
    }
    public boolean complete(Claim c,String key,Runnable upload) {
        return Boolean.TRUE.equals(store.tx.execute(tx -> {
            if(!lock(c)) return false;
            // The job lock serializes publication with deletion, including the S3 write.
            upload.run();
            store.jdbc.update("UPDATE documents SET status='SUCCEEDED',result_key=?,error_code=NULL,lease_until=NULL,updated_at=now() WHERE id=? AND token=?",key,c.id(),c.token());
            store.jdbc.update("UPDATE processing_attempts SET finished_at=now() WHERE token=?",c.token());
            refresh(c.jobId()); return true;
        }));
    }
    public boolean fail(Claim c,String code,boolean retryable) {
        return Boolean.TRUE.equals(store.tx.execute(tx -> {
            if(!lock(c)) return false;
            boolean retry=retryable && c.attempt()<3;
            int delay=c.attempt()==1 ? 30 : 120;
            store.jdbc.update("UPDATE documents SET status=?,error_code=?,lease_until=NULL,available_at=now()+(? * interval '1 second'),updated_at=now() WHERE id=?",retry?"RETRY_WAIT":"FAILED",code,delay,c.id());
            store.jdbc.update("UPDATE processing_attempts SET finished_at=now(),error_code=? WHERE token=?",code,c.token());
            if(retry) store.jdbc.update("INSERT INTO outbox(document_id,available_at) VALUES (?,now()+(? * interval '1 second')) ON CONFLICT(document_id) DO UPDATE SET available_at=EXCLUDED.available_at",c.id(),delay);
            refresh(c.jobId()); return !retry;
        }));
    }
    private boolean lock(Claim c) {
        var jobs=store.jdbc.queryForList("SELECT status FROM jobs WHERE id=? FOR UPDATE",c.jobId());
        if(jobs.isEmpty() || "DELETING".equals(jobs.getFirst().get("status"))) return false;
        return Boolean.TRUE.equals(store.jdbc.queryForObject("SELECT EXISTS(SELECT 1 FROM documents WHERE id=? AND token=? AND status='PROCESSING' AND lease_until>now())",Boolean.class,c.id(),c.token()));
    }
    public void refresh(UUID job) {
        store.jdbc.update("""
            UPDATE jobs SET status=CASE
              WHEN EXISTS(SELECT 1 FROM documents WHERE job_id=? AND status NOT IN ('SUCCEEDED','FAILED')) THEN 'PROCESSING'
              WHEN NOT EXISTS(SELECT 1 FROM documents WHERE job_id=? AND status<>'SUCCEEDED') THEN 'SUCCEEDED'
              WHEN NOT EXISTS(SELECT 1 FROM documents WHERE job_id=? AND status='SUCCEEDED') THEN 'FAILED'
              ELSE 'PARTIAL_SUCCESS' END,updated_at=now()
            WHERE id=? AND status<>'DELETING'
            """,job,job,job,job);
    }
    public void recover() {
        var jobs=store.jdbc.queryForList("SELECT DISTINCT job_id FROM documents WHERE status='PROCESSING' AND lease_until<now()");
        for(var row:jobs) store.tx.executeWithoutResult(tx -> {
            UUID job=(UUID)row.get("job_id");
            var states=store.jdbc.queryForList("SELECT status FROM jobs WHERE id=? FOR UPDATE",job);
            if(states.isEmpty() || "DELETING".equals(states.getFirst().get("status"))) return;
            store.jdbc.update("UPDATE processing_attempts SET finished_at=now(),error_code='LEASE_EXPIRED' WHERE token IN (SELECT token FROM documents WHERE job_id=? AND status='PROCESSING' AND lease_until<now())",job);
            store.jdbc.update("UPDATE documents SET status=CASE WHEN attempts>=3 THEN 'FAILED' ELSE 'RETRY_WAIT' END,error_code='LEASE_EXPIRED',lease_until=NULL,available_at=now(),updated_at=now() WHERE job_id=? AND status='PROCESSING' AND lease_until<now()",job);
            refresh(job);
        });
        store.jdbc.update("""
            INSERT INTO outbox(document_id,available_at)
            SELECT d.id,d.available_at FROM documents d JOIN jobs j ON j.id=d.job_id
            WHERE j.status IN ('QUEUED','PROCESSING') AND d.status IN ('QUEUED','RETRY_WAIT')
              AND (d.last_enqueued_at IS NULL OR d.last_enqueued_at<now()-interval '2 minutes')
            ON CONFLICT(document_id) DO NOTHING
            """);
    }
    public void dispatch(Queue queue) {
        // A crash after XADD but before commit duplicates delivery; claim/completion are idempotent.
        for(int batch=0;batch<40;batch++) {
            Integer count=store.tx.execute(tx -> {
                var rows=store.jdbc.queryForList("SELECT document_id FROM outbox WHERE available_at<=now() ORDER BY available_at LIMIT 100 FOR UPDATE SKIP LOCKED");
                for(var row:rows) {
                    UUID id=(UUID)row.get("document_id"); queue.publish(id);
                    store.jdbc.update("UPDATE documents SET last_enqueued_at=now() WHERE id=?",id);
                    store.jdbc.update("DELETE FROM outbox WHERE document_id=?",id);
                }
                return rows.size();
            });
            if(count==null || count<100) break;
        }
    }
}
