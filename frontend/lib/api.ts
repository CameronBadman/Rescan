// The Rescan API as the frontend sees it. Base URL and key are baked in at
// build time (NEXT_PUBLIC_*). Every request carries the frontend's own key.

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
export type JobSummary = { id: string; status: string; created_at: string; updated_at: string }
export type JobDetail = {
  job_id: string; status: string; role: { title: string; description?: string | null } | null
  created_at: string; updated_at: string; error: string | null; has_shortlist: boolean; has_rules: boolean
  counts: Counts; total: number
}
export type JobStatus = { job_id: string; status: string; counts: Counts; total: number; processed: number; error: string | null }
export type Criterion = { criterion: string; score: number; weight: number; evidence: string }
export type Identity = { full_name?: string | null; email?: string | null; phone?: string | null } | null
export type Entry = { rank: number; candidate_ref: string; score: number; rationale: string; borderline: boolean; criteria: Criterion[]; identity?: Identity }
export type Excluded = { candidate_ref: string; reasons: string[]; failed_rules: string[]; identity?: Identity }
export type ManualReview = { candidate_ref: string; reasons: string[]; identity?: Identity }
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
export type AuditEntry = { id: number; at: string; stage: string; event: string; candidate_id: string | null; detail: Record<string, any> }
export type QueryResult = {
  canonical: string; counts: Counts
  matched: { candidate_ref: string; reason: string }[]; not_matched: { candidate_ref: string; reason: string }[]; indeterminate: { candidate_ref: string; reason: string }[]
}

// ---- calls ----------------------------------------------------------------

export const api = {
  health: () => request<{ status: string; llm_backend: string }>('/health'),
  listJobs: () => request<{ jobs: JobSummary[] }>('/jobs?limit=50'),
  job: (id: string) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}`),
  status: (id: string) => request<JobStatus>(`/jobs/${encodeURIComponent(id)}/status`),
  shortlist: (id: string, reattach = false) =>
    request<Shortlist>(`/jobs/${encodeURIComponent(id)}/shortlist?reattach_identity=${reattach}`),
  rules: (id: string) => request<RuleSet>(`/jobs/${encodeURIComponent(id)}/rules`),
  audit: (id: string, limit = 500) => request<{ entries: AuditEntry[] }>(`/jobs/${encodeURIComponent(id)}/audit?limit=${limit}`),
  checkRules: (rules: string[], roleContext?: string) =>
    request<RuleSet>('/rules/check', { method: 'POST', body: JSON.stringify({ rules, role_context: roleContext ?? null }) }),
  compilePlan: (plan: string, roleContext?: string) =>
    request<RuleSet>('/rules/compile', { method: 'POST', body: JSON.stringify({ plan, role_context: roleContext ?? null }) }),
  query: (id: string, dsl: string, modelChecks = true) =>
    request<QueryResult>(`/jobs/${encodeURIComponent(id)}/query`, { method: 'POST', body: JSON.stringify({ dsl, model_checks: modelChecks }) }),
  addRule: (id: string, text: string) =>
    request<{ rule: Rule; added: boolean; rescreening: boolean }>(`/jobs/${encodeURIComponent(id)}/rules`, { method: 'POST', body: JSON.stringify({ text }) }),
  removeRule: (id: string, ruleId: string) =>
    request<{ removed: string; rescreening: boolean }>(`/jobs/${encodeURIComponent(id)}/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' }),
  fromBucket: (jobId: string, role: { title: string; description?: string }, plan: string) =>
    request<{ job_id: string; accepted_documents: number }>('/jobs/from-bucket', {
      method: 'POST', body: JSON.stringify({ job_id: jobId, role, plan, rules: [] }),
    }),
  upload: (files: File[], role: { title: string; description?: string }, plan: string) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    form.append('role', JSON.stringify(role))
    form.append('rules', '[]')
    form.append('plan', plan)
    return request<{ job_id: string; accepted_documents: number }>('/jobs', { method: 'POST', body: form })
  },
}

export const settled = (status: string) => status === 'complete' || status === 'failed'
