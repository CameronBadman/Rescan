/** Browser integration. Files retain manifest order, including duplicate names.
 * Persist idempotencyKey and reuse it when retrying the same manifest.
 */
export async function uploadBatch(baseUrl, accessToken, files, idempotencyKey, onProgress = () => {}) {
  if (files.length < 1 || files.length > 4000) throw new Error("Select 1–4000 resumes");
  const request = async (path, options = {}) => {
    const response = await fetch(`${baseUrl}/v1/jobs${path}`, {
      ...options,
      headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json", ...options.headers },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(JSON.stringify(body));
    return body;
  };
  const created = await request("", { method: "POST", headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ files: files.map(file => ({ filename: file.name, sizeBytes: file.size })) }) });
  const jobId = created.jobId;
  let page = created;
  if (page.uploads) {
    while (true) {
      const pending = [...page.uploads];
      await Promise.all(Array.from({ length: 8 }, async () => {
        while (pending.length) {
          const upload = pending.shift();
          const headers = Object.fromEntries(Object.entries(upload.headers).map(([key, values]) => [key, values.join(",")]));
          const response = await fetch(upload.url, { method: "PUT", headers, body: files[upload.fileIndex] });
          // 412 means this immutable upload already exists from an earlier attempt.
          if (!response.ok && response.status !== 412) throw new Error(`Upload failed: ${upload.documentId} (${response.status}); refresh upload URLs and retry`);
        }
      }));
      if (!page.nextCursor) break;
      page = await request(`/${jobId}/upload-urls?after=${page.nextCursor}`, { method: "POST" });
    }
    await request(`/${jobId}/submit`, { method: "POST" });
  }
  while (true) {
    const job = await request(`/${jobId}`);
    onProgress(job);
    if (job.status === "UPLOADING") throw new Error(`Upload verification failed for ${jobId}; inspect document error_code values, correct uploads, and submit again.`);
    if (["SUCCEEDED", "PARTIAL_SUCCESS", "FAILED", "DELETING"].includes(job.status)) return job;
    await new Promise(resolve => setTimeout(resolve, 5000));
  }
}
