// The Rescan API as the frontend sees it. Base URL and key are baked in at
// build time (NEXT_PUBLIC_*). Every request carries the frontend's own key.
//
// Two resources: a **batch** is a set of resumes, ingested and de-identified
// once; an **analysis run** applies one rule set to a batch and owns the
// outcomes, the shortlist and its own audit trail.

export const API_URL = (process.env.NEXT_PUBLIC_RESCAN_API_URL || 'http://localhost:8080').replace(/\/$/, '')
const API_KEY = process.env.NEXT_PUBLIC_RESCAN_API_KEY || ''

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : (detail as any)?.message || (detail as any)?.detail?.message || `API ${status}`)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {})
  if (API_KEY) headers.set('X-API-Key', API_KEY)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_URL}${path}`, { ...init, headers })
  const text = await response.text()
  let body: any = null
  try { body = text ? JSON.parse(text) : null } catch { body = text }
  if (!response.ok) throw new ApiError(response.status, body?.detail ?? body)
  return body as T
}

// ---- types (mirroring rescan/schemas.py and the API payloads) --------------

export type Counts = Record<string, number>
export type Role = { title: string; description?: string | null }

export type BatchSummary = { id: string; name: string | null; status: string; total: number; ready: number; runs: number; created_at: string; updated_at: string }
export type BatchDetail = {
  batch_id: string; name: string | null; status: string; counts: Counts; total: number; ready: number
  source: Record<string, any> | null; runs: RunSummary[]; created_at: string; updated_at: string; error: string | null
}

export type RunSummary = { id: string; batch_id: string; name: string | null; status: string; role_title: string | null; has_shortlist: boolean; created_at: string; updated_at: string }
export type RunDetail = {
  run_id: string; batch_id: string; batch_name: string | null; batch_status: string | null
  name: string | null; status: string; role: Role | null; counts: Counts; screened: number; total: number
  has_shortlist: boolean; has_rules: boolean; created_at: string; updated_at: string; error: string | null
}

export type Criterion = { criterion: string; score: number; weight: number; evidence: string }
export type Identity = { full_name?: string | null; email?: string | null; phone?: string | null } | null
export type CandidateDocument = { candidate_id: string; candidate_ref: string; filename: string; source: 'extracted' | 'profile_summary'; has_document: boolean; text: string }
export type Entry = { rank: number; candidate_ref: string; candidate_id?: string | null; score: number; rationale: string; borderline: boolean; criteria: Criterion[]; identity?: Identity }
export type Excluded = { candidate_ref: string; candidate_id?: string | null; reasons: string[]; failed_rules: string[]; identity?: Identity }
export type ManualReview = { candidate_ref: string; candidate_id?: string | null; reasons: string[]; identity?: Identity }
export type Shortlist = { role_title: string; entries: Entry[]; below_cutoff: Entry[]; excluded: Excluded[]; manual_review: ManualReview[] }

export type Finding = {
  pattern_id: string; risk: 'none' | 'review' | 'high'; matched_text: string | null
  protected_attributes: string[]; statutes: string[]; explanation: string; suggested_rewrite: string; source: string
}
export type Rule = {
  id: string; source_text: string; verdict: 'applicable' | 'risky' | 'unmappable'; risk: 'none' | 'review' | 'high'
  findings: Finding[]; dsl: string | null; kind: 'require' | 'prefer'; justification: string | null; notes: string[]
}
export type RuleSet = { rules: Rule[]; reasoning: string | null; source_plan: string | null; applied: number; flagged: number; requirements?: number; preferences?: number }
export type AuditEntry = { id: number; at: string; stage: string; event: string; batch_id: string; run_id: string | null; candidate_id: string | null; detail: Record<string, any> }
export type QueryResult = {
  canonical: string; counts: Counts
  matched: { candidate_ref: string; reason: string }[]; not_matched: { candidate_ref: string; reason: string }[]; indeterminate: { candidate_ref: string; reason: string }[]
}

// ---- calls ----------------------------------------------------------------

export const api = {
  health: () => request<{ status: string; llm_backend: string }>('/health'),

  // batches: resumes in, anonymized profiles out
  listBatches: () => request<{ batches: BatchSummary[] }>('/batches?limit=50'),
  batch: (id: string) => request<BatchDetail>(`/batches/${encodeURIComponent(id)}`),
  batchCandidates: (id: string) => request<{ candidates: any[] }>(`/batches/${encodeURIComponent(id)}/candidates`),
  batchAudit: (id: string, limit = 500) => request<{ entries: AuditEntry[] }>(`/batches/${encodeURIComponent(id)}/audit?limit=${limit}`),
  query: (batchId: string, dsl: string, modelChecks = true) =>
    request<QueryResult>(`/batches/${encodeURIComponent(batchId)}/query`, { method: 'POST', body: JSON.stringify({ dsl, model_checks: modelChecks }) }),
  batchFromBucket: (batchId: string, name?: string) =>
    request<{ batch_id: string; accepted_documents: number }>('/batches/from-bucket', {
      method: 'POST', body: JSON.stringify({ batch_id: batchId, name: name || null }),
    }),
  uploadBatch: (files: File[], name?: string) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    if (name) form.append('name', name)
    return request<{ batch_id: string; accepted_documents: number }>('/batches', { method: 'POST', body: form })
  },

  // analysis runs: a rule set applied to a batch
  listRuns: (q?: string, batchId?: string) => {
    const params = new URLSearchParams({ limit: '50' })
    if (q) params.set('q', q)
    if (batchId) params.set('batch_id', batchId)
    return request<{ runs: RunSummary[] }>(`/runs?${params}`)
  },
  run: (id: string) => request<RunDetail>(`/runs/${encodeURIComponent(id)}`),
  createRun: (batchId: string, role: Role, opts: { name?: string; plan?: string; rulesFrom?: string } = {}) =>
    request<{ run_id: string; batch_id: string }>('/runs', {
      method: 'POST',
      body: JSON.stringify({
        batch_id: batchId, role, name: opts.name || null,
        plan: opts.rulesFrom ? null : opts.plan || null, rules: [], rules_from: opts.rulesFrom || null,
      }),
    }),
  runRules: (id: string) => request<RuleSet>(`/runs/${encodeURIComponent(id)}/rules`),
  addRule: (id: string, text: string) =>
    request<{ rule: Rule; added: boolean; rescreening: boolean }>(`/runs/${encodeURIComponent(id)}/rules`, { method: 'POST', body: JSON.stringify({ text }) }),
  removeRule: (id: string, ruleId: string) =>
    request<{ removed: string; rescreening: boolean }>(`/runs/${encodeURIComponent(id)}/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' }),
  runShortlist: (id: string, reattach = false) =>
    request<Shortlist>(`/runs/${encodeURIComponent(id)}/shortlist?reattach_identity=${reattach}`),
  runAudit: (id: string, includeBatch = true, limit = 500) =>
    request<{ entries: AuditEntry[] }>(`/runs/${encodeURIComponent(id)}/audit?include_batch=${includeBatch}&limit=${limit}`),

  // the document behind a candidate, for a reviewer who opens them
  candidateDocument: async (candidateId: string): Promise<{ kind: 'file'; url: string; type: string } | { kind: 'text'; doc: CandidateDocument }> => {
    const headers = new Headers()
    if (API_KEY) headers.set('X-API-Key', API_KEY)
    const response = await fetch(`${API_URL}/candidates/${encodeURIComponent(candidateId)}/document`, { headers })
    if (!response.ok) throw new ApiError(response.status, await response.text())
    const type = response.headers.get('content-type') || ''
    if (type.includes('application/json')) return { kind: 'text', doc: await response.json() }
    return { kind: 'file', url: URL.createObjectURL(await response.blob()), type }
  },

  // rule checking, independent of any run
  checkRules: (rules: string[], roleContext?: string) =>
    request<RuleSet>('/rules/check', { method: 'POST', body: JSON.stringify({ rules, role_context: roleContext ?? null }) }),
  compilePlan: (plan: string, roleContext?: string) =>
    request<RuleSet>('/rules/compile', { method: 'POST', body: JSON.stringify({ plan, role_context: roleContext ?? null }) }),
}

export const settled = (status: string | undefined) => status === 'complete' || status === 'failed'
