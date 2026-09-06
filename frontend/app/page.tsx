'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowUpRight, BarChart3, Check, ChevronDown, CircleHelp, ClipboardCheck, FileCheck2, Filter, Info, Layers3, LockKeyhole, MoreHorizontal, Plus, Search, ShieldCheck, Sparkles, Trash2, Upload, Users, WandSparkles, X } from 'lucide-react'
import { api, ApiError, API_URL, settled } from '@/lib/api'
import type { AuditEntry, BatchDetail, BatchSummary, Entry, Identity, Rule, RuleSet, RunDetail, RunSummary, Shortlist } from '@/lib/api'

type View = 'Overview' | 'Candidates' | 'Analysis runs' | 'Batches' | 'Audit trail'

const navItems: [View, typeof BarChart3][] = [
  ['Overview', BarChart3], ['Candidates', Users], ['Analysis runs', ShieldCheck], ['Batches', Layers3], ['Audit trail', FileCheck2],
]

// One row of the candidate table, built from the run's shortlist sections.
type Row = {
  ref: string; score: number | null; status: string; tone: 'good' | 'warn' | 'bad' | 'muted'
  evidence: string; section: 'shortlist' | 'below_cutoff' | 'manual_review' | 'excluded'
}

function rowsFor(shortlist: Shortlist | null): Row[] {
  if (!shortlist) return []
  const scored = (entry: Entry, section: Row['section']): Row => ({
    ref: entry.candidate_ref,
    score: entry.score,
    status: section === 'shortlist' ? (entry.borderline ? 'Borderline' : 'Strong match') : 'Below cutoff',
    tone: section === 'shortlist' ? (entry.borderline ? 'warn' : 'good') : 'muted',
    evidence: entry.criteria.filter((c) => c.score > 0).map((c) => c.criterion.replace(/_/g, ' ')).slice(0, 3).join(' · ') || entry.rationale,
    section,
  })
  return [
    ...shortlist.entries.map((e) => scored(e, 'shortlist')),
    ...shortlist.below_cutoff.map((e) => scored(e, 'below_cutoff')),
    ...shortlist.manual_review.map((m) => ({ ref: m.candidate_ref, score: null, status: 'Review needed', tone: 'warn' as const, evidence: m.reasons[0] || '', section: 'manual_review' as const })),
    ...shortlist.excluded.map((x) => ({ ref: x.candidate_ref, score: null, status: 'Excluded', tone: 'bad' as const, evidence: x.reasons[0] || x.failed_rules[0] || '', section: 'excluded' as const })),
  ]
}

const toneClass = { good: 'bg-[#e8f1e9] text-[#47765f]', warn: 'bg-[#fff1df] text-[#bd783d]', bad: 'bg-[#fbe5e1] text-[#a4453a]', muted: 'bg-[#eef1ef] text-[#6f7e76]' }
const statusClass = (status: string) => status === 'complete' ? 'bg-[#e8f1e9] text-[#47765f]' : status === 'failed' ? 'bg-[#fbe5e1] text-[#a4453a]' : 'bg-[#fff1df] text-[#bd783d]'

function timeAgo(iso: string) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

function describeEvent(entry: AuditEntry): { title: string; desc: string } {
  const d = entry.detail || {}
  switch (entry.event) {
    case 'batch_created': return { title: 'Batch created', desc: `${d.accepted ?? 0} documents accepted${d.duplicates ? `, ${d.duplicates} duplicates skipped` : ''}` }
    case 'bucket_pulled': return { title: 'Resumes pulled from storage', desc: `${d.documents} documents from ${d.prefix}` }
    case 'batch_complete': return { title: 'Batch ready', desc: `${d.ready} profiles ready to analyse${d.needs_manual_review ? `, ${d.needs_manual_review} need a human` : ''}` }
    case 'batch_failed': return { title: 'Batch failed', desc: d.error }
    case 'run_created': return { title: 'Analysis run created', desc: `${d.name || d.run_id} over ${d.candidates} candidates for ${d.role}` }
    case 'run_complete': return { title: 'Analysis complete', desc: `${d.shortlisted} shortlisted, ${d.excluded} excluded with reasons` }
    case 'run_failed': return { title: 'Analysis failed', desc: d.error }
    case 'plan_compiled': return { title: 'Hiring plan compiled', desc: `${d.rules} requirements read: ${d.applied} applied, ${d.flagged} flagged` }
    case 'rules_copied': return { title: 'Rules reused', desc: `${d.rules} rules copied from ${d.from_run}` }
    case 'rule_risk_flagged': return { title: 'Rule flagged', desc: `“${d.text}” — ${(d.statutes || [])[0] || d.pattern}` }
    case 'rule_applicable': return { title: 'Rule applied', desc: d.dsl || d.text }
    case 'rule_unmappable': return { title: 'Rule sent to a human', desc: d.text }
    case 'rule_risky': return { title: 'Rule not applied', desc: d.text }
    case 'rule_added': return { title: 'Rule added', desc: d.dsl || d.text }
    case 'rule_removed': return { title: 'Rule removed', desc: d.text }
    case 'rule_rejected': return { title: 'Rule refused', desc: `“${d.text}” screens on a protected attribute` }
    case 'rescreen_started': return { title: 'Re-screening', desc: `${d.applied} rules applied to the stored profiles` }
    case 'rescreen_complete': return { title: 'Re-screened', desc: `${d.candidates} candidates, ${d.shortlisted} shortlisted` }
    case 'rule_failed': return { title: 'Candidate excluded by a rule', desc: d.reason }
    case 'rule_indeterminate': return { title: 'Rule could not be decided', desc: d.reason }
    case 'model_check': return { title: `Model check: ${d.answer}`, desc: d.question }
    case 'redaction': return { title: 'Identity removed', desc: `${d.field}: ${d.reason}` }
    case 'scored': return { title: `${d.candidate_ref} scored ${Number(d.score).toFixed(2)}`, desc: d.rationale || '' }
    case 'ensemble_started': return { title: 'Second opinion requested', desc: `${(d.candidates || []).length} borderline candidates re-scored by the ensemble` }
    case 'query_run': return { title: 'Query run', desc: `${d.canonical} → ${d.counts?.matched ?? 0} matched` }
    case 'dead_lettered': return { title: 'Document needs manual review', desc: d.reason }
    default: return { title: entry.event.replace(/_/g, ' '), desc: Object.keys(d).slice(0, 4).map((k) => `${k}: ${JSON.stringify(d[k])}`).join(', ') }
  }
}

const runLabel = (run: RunSummary | RunDetail | null) => {
  if (!run) return '—'
  const id = 'run_id' in run ? run.run_id : run.id
  const role = 'role_title' in run ? run.role_title : run.role?.title
  return run.name || `${role ?? 'Run'} · ${id.slice(0, 12)}`
}

export default function Page() {
  const [activeNav, setActiveNav] = useState<View>('Overview')
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [batches, setBatches] = useState<BatchSummary[]>([])
  const [runId, setRunId] = useState<string | null>(null)
  const [run, setRun] = useState<RunDetail | null>(null)
  const [batch, setBatch] = useState<BatchDetail | null>(null)
  const [shortlist, setShortlist] = useState<Shortlist | null>(null)
  const [rules, setRules] = useState<RuleSet | null>(null)
  const [audit, setAudit] = useState<AuditEntry[]>([])
  const [identities, setIdentities] = useState<Record<string, Identity>>({})
  const [error, setError] = useState<string | null>(null)
  const [showNewRun, setShowNewRun] = useState(false)
  const [showNewBatch, setShowNewBatch] = useState(false)
  const [query, setQuery] = useState('')
  const [version, setVersion] = useState(0)
  const reload = () => setVersion((v) => v + 1)

  const refresh = useCallback(async () => {
    try {
      const [runsBody, batchesBody] = await Promise.all([api.listRuns(), api.listBatches()])
      setRuns(runsBody.runs)
      setBatches(batchesBody.batches)
      setRunId((current) => {
        if (current && runsBody.runs.some((r) => r.id === current)) return current
        const remembered = typeof window !== 'undefined' ? window.localStorage.getItem('rescan.run') : null
        return (remembered && runsBody.runs.some((r) => r.id === remembered) ? remembered : runsBody.runs[0]?.id) ?? null
      })
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError && e.status === 401 ? 'The API rejected the frontend key.' : `Cannot reach the API at ${API_URL}.`)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh, version])

  // Poll the selected run while it works; load its results once it settles.
  useEffect(() => {
    if (!runId) { setRun(null); setBatch(null); setShortlist(null); setRules(null); setAudit([]); return }
    if (typeof window !== 'undefined') window.localStorage.setItem('rescan.run', runId)
    let cancelled = false
    setShortlist(null); setRules(null); setAudit([]); setIdentities({})
    const tick = async () => {
      try {
        const detail = await api.run(runId)
        if (cancelled) return
        setRun(detail)
        api.batch(detail.batch_id).then((b) => { if (!cancelled) setBatch(b) }).catch(() => {})
        if (settled(detail.status)) {
          const [sl, rs, au] = await Promise.all([
            detail.has_shortlist ? api.runShortlist(runId) : Promise.resolve(null),
            detail.has_rules ? api.runRules(runId) : Promise.resolve(null),
            api.runAudit(runId),
          ])
          if (cancelled) return
          setShortlist(sl); setRules(rs); setAudit(au.entries)
          return
        }
        setTimeout(tick, 2000)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    }
    tick()
    return () => { cancelled = true }
  }, [runId, version])

  const rows = useMemo(() => rowsFor(shortlist), [shortlist])
  const filteredRows = rows.filter((r) => `${r.ref} ${r.status} ${r.evidence}`.toLowerCase().includes(query.toLowerCase()))
  const roleTitle = run?.role?.title ?? shortlist?.role_title ?? '—'
  const go = (view: View) => setActiveNav(view)
  const running = run ? !settled(run.status) : false

  // "Review" re-attaches identity: the one place a human sees a name.
  const review = async (ref: string) => {
    if (!runId || identities[ref] !== undefined) return
    const identified = await api.runShortlist(runId, true)
    const all = [...identified.entries, ...identified.below_cutoff, ...identified.excluded, ...identified.manual_review]
    setIdentities((current) => ({ ...current, [ref]: all.find((e) => e.candidate_ref === ref)?.identity ?? null }))
  }

  return <main className="min-h-screen bg-[#f7f8f6] text-[#19312b]">
    <aside className="fixed inset-y-0 left-0 hidden w-64 flex-col border-r border-[#dfe7e1] bg-[#fbfcfa] px-5 py-6 lg:flex">
      <Brand />
      <div className="mt-12 px-2 text-[10px] font-bold uppercase tracking-[0.16em] text-[#8a9991]">Workspace</div>
      <nav className="mt-3 flex flex-col gap-1">{navItems.map(([label, Icon]) => <button key={label} onClick={() => go(label)} className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm transition ${activeNav === label ? 'bg-[#e8f0ea] font-semibold' : 'text-[#6f7e76] hover:bg-[#f0f4f0]'}`}><Icon size={17} strokeWidth={1.8} /><span>{label}</span>{label === 'Candidates' && run && <span className="ml-auto rounded-full bg-[#dce9df] px-2 py-0.5 text-[10px] font-bold">{run.screened}</span>}{label === 'Batches' && batches.length > 0 && <span className="ml-auto rounded-full bg-[#dce9df] px-2 py-0.5 text-[10px] font-bold">{batches.length}</span>}</button>)}</nav>
      <div className="mt-auto rounded-xl border border-[#dfe7e1] bg-[#f1f6f1] p-4"><div className="flex items-center gap-2 text-xs font-semibold"><LockKeyhole size={14} /> Privacy-first by design</div><p className="mt-2 text-xs leading-5 text-[#73827a]">Identities stay hidden until a human opens a candidate.</p><a href={`${API_URL}/docs`} target="_blank" rel="noreferrer" className="mt-3 text-xs font-bold text-[#47765f]">API reference <ArrowUpRight className="ml-1 inline" size={13} /></a></div>
      <div className="mt-5 flex items-center gap-3 border-t border-[#dfe7e1] pt-5"><div className="flex h-8 w-8 items-center justify-center rounded-full bg-[#e9c7a9] text-xs font-bold">RT</div><div><p className="text-xs font-bold">Recruiting team</p><p className="text-[11px] text-[#839088]">{runs.length} analysis runs</p></div><MoreHorizontal className="ml-auto text-[#9aa59e]" size={17} /></div>
    </aside>
    <section className="lg:ml-64">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[#dfe7e1] bg-[#fbfcfa] px-6 py-5 md:px-10">
        <div><p className="text-xs font-semibold text-[#829088]">{new Date().toLocaleDateString('en-AU', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}</p><h1 className="mt-1 text-2xl font-semibold tracking-tight">Good {new Date().getHours() < 12 ? 'morning' : 'afternoon'}</h1></div>
        <div className="flex items-center gap-3"><button aria-label="Help" className="rounded-full p-2 text-[#718078] hover:bg-[#eef3ee]"><CircleHelp size={19} /></button><RunPicker runs={runs} runId={runId} onSelect={setRunId} onNew={() => setShowNewRun(true)} /></div>
      </header>
      <div className="mx-auto max-w-[1220px] space-y-7 px-6 py-8 md:px-10">
        {error && <div className="rounded-lg border border-[#e6c3b8] bg-[#fdf1ec] p-3 text-xs text-[#8a4a3d]">{error}</div>}
        {activeNav === 'Overview' && <Overview go={go} run={run} batch={batch} shortlist={shortlist} audit={audit} running={running} roleTitle={roleTitle} onRuleAdded={reload} onNewRun={() => setShowNewRun(true)} hasRuns={runs.length > 0} />}
        {activeNav === 'Candidates' && <CandidatesView rows={filteredRows} run={run} query={query} setQuery={setQuery} identities={identities} onReview={review} />}
        {activeNav === 'Analysis runs' && <RunsView runs={runs} runId={runId} onSelect={setRunId} run={run} rules={rules} running={running} roleTitle={roleTitle} onChanged={reload} onNew={() => setShowNewRun(true)} />}
        {activeNav === 'Batches' && <BatchesView batches={batches} onSelectRun={(id) => { setRunId(id); go('Overview') }} onNew={() => setShowNewBatch(true)} onRefresh={reload} />}
        {activeNav === 'Audit trail' && <AuditView audit={audit} rules={rules} run={run} reviewed={Object.keys(identities).length} roleTitle={roleTitle} />}
      </div>
    </section>
    {showNewRun && <NewRunModal batches={batches} runs={runs} onClose={() => setShowNewRun(false)} onCreated={async (id) => { setShowNewRun(false); await refresh(); setRunId(id); setActiveNav('Overview') }} />}
    {showNewBatch && <NewBatchModal onClose={() => setShowNewBatch(false)} onCreated={async () => { setShowNewBatch(false); await refresh(); setActiveNav('Batches') }} />}
  </main>
}

function Brand() { return <div className="flex items-center gap-2 px-2"><div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#19312b] text-[#e7f2e8]"><ShieldCheck size={18} /></div><span className="font-mono text-sm font-bold tracking-tight">fairmatch<span className="text-[#ee8b57]">.</span></span></div> }
function PageHead({ eyebrow, title, body, action }: { eyebrow: string; title: React.ReactNode; body: string; action?: React.ReactNode }) { return <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end"><div><p className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.14em] text-[#6c8175]"><span className="h-2 w-2 rounded-full bg-[#5d9b72]" /> {eyebrow}</p><h2 className="mt-2 text-3xl font-semibold tracking-tight">{title}</h2><p className="mt-2 max-w-2xl text-sm leading-6 text-[#819088]">{body}</p></div>{action}</div> }

// The header selector: search every analysis run by name, role or batch.
function RunPicker({ runs, runId, onSelect, onNew }: { runs: RunSummary[]; runId: string | null; onSelect: (id: string) => void; onNew: () => void }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const current = runs.find((r) => r.id === runId) ?? null
  const shown = runs.filter((r) => `${r.name ?? ''} ${r.role_title ?? ''} ${r.batch_id} ${r.id}`.toLowerCase().includes(search.toLowerCase()))
  return <div className="relative">
    <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-2 rounded-lg border border-[#dce5de] bg-white px-3 py-2 text-xs font-semibold">
      <Search size={13} className="text-[#91a099]" />{current ? runLabel(current) : 'No analysis run'} <ChevronDown size={14} />
    </button>
    {open && <div className="absolute right-0 top-12 z-20 w-80 rounded-lg border border-[#dfe7e1] bg-white p-2 shadow-lg">
      <input autoFocus value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search runs by name, role or batch" className="mb-2 w-full rounded-md bg-[#f5f8f5] px-3 py-2 text-xs outline-none" />
      <div className="max-h-72 overflow-y-auto">
        {shown.map((r) => <button key={r.id} onClick={() => { onSelect(r.id); setOpen(false); setSearch('') }} className={`flex w-full flex-col rounded-md px-3 py-2 text-left hover:bg-[#f0f4f0] ${r.id === runId ? 'bg-[#e8f0ea]' : ''}`}>
          <span className="flex items-center justify-between text-xs font-semibold">{runLabel(r)}<span className={`rounded-full px-2 py-0.5 text-[10px] ${statusClass(r.status)}`}>{r.status}</span></span>
          <span className="mt-0.5 text-[11px] text-[#8a9890]">{r.role_title ?? 'no role'} · batch {r.batch_id} · {timeAgo(r.created_at)}</span>
        </button>)}
        {shown.length === 0 && <p className="p-3 text-xs text-[#819088]">No runs match.</p>}
      </div>
      <button onClick={() => { setOpen(false); onNew() }} className="mt-2 flex w-full items-center justify-center gap-2 rounded-md bg-[#19312b] px-3 py-2 text-xs font-semibold text-white"><Plus size={14} /> New analysis run</button>
    </div>}
  </div>
}

function Overview({ go, run, batch, shortlist, audit, running, roleTitle, onRuleAdded, onNewRun, hasRuns }: any) {
  if (!run) return <><PageHead eyebrow="Overview" title="No analysis run selected" body={hasRuns ? 'Choose a run from the picker above, or start a new one.' : 'Add a batch of resumes first, then analyse it with a hiring plan.'} action={<button onClick={onNewRun} className="flex w-fit items-center gap-2 rounded-lg bg-[#19312b] px-4 py-2.5 text-sm font-semibold text-white"><Plus size={17} /> New analysis run</button>} />
    <div className="rounded-xl border border-[#dfe7e1] bg-white p-8 text-center text-sm text-[#819088]">Nothing to show yet. <button onClick={() => go('Batches')} className="font-bold text-[#507663]">Add a batch of resumes</button> to begin.</div></>

  const shortlisted = shortlist?.entries.length ?? 0
  const manual = run.counts?.needs_manual_review ?? 0
  const tiles: [string, string, typeof Users, number][] = [
    [String(batch?.total ?? run.total), 'Resumes in the batch', Users, 100],
    [String(run.screened), running ? 'Screened so far' : 'Screened', Sparkles, run.total ? Math.round((run.screened / run.total) * 100) : 0],
    [String(shortlisted), 'Shortlisted', Check, run.total ? Math.round((shortlisted / run.total) * 100) : 0],
    [String(manual), 'Need a closer look', Info, run.total ? Math.round((manual / run.total) * 100) : 0],
  ]
  return <><PageHead eyebrow={running ? 'Analysis in progress' : 'Analysis run'} title={<>{roleTitle} <span className="text-[#8c9991]">/ {run.name || run.run_id}</span></>} body={running ? `Screening ${run.screened} of ${run.total} candidates against this run's rules.` : `Batch ${run.batch_name || run.batch_id} · ${run.total} candidates · created ${timeAgo(run.created_at)}.`} action={<button onClick={onNewRun} className="flex w-fit items-center gap-2 rounded-lg bg-[#19312b] px-4 py-2.5 text-sm font-semibold text-white"><Plus size={17} /> New analysis run</button>} />
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{tiles.map(([value, label, Icon, pct], i) => <div key={label} className="rounded-xl border border-[#dfe7e1] bg-white p-5"><div className="flex items-start justify-between"><span className="text-3xl font-semibold">{value}</span><div className={`rounded-lg p-2 ${i === 3 ? 'bg-[#fff1df] text-[#bd783d]' : 'bg-[#e8f1e9] text-[#47765f]'}`}><Icon size={17} /></div></div><p className="mt-4 text-xs font-medium text-[#7d8a82]">{label}</p><div className="mt-3 h-1 rounded-full bg-[#edf1ed]"><div className={`h-1 rounded-full ${i === 3 ? 'bg-[#e2a264]' : 'bg-[#70a781]'}`} style={{ width: `${pct}%` }} /></div></div>)}</div>
    <div className="grid gap-6 xl:grid-cols-[1.4fr_1fr]">
      <section className="rounded-xl border border-[#dfe7e1] bg-white p-6"><div className="flex justify-between"><div><h3 className="font-semibold">Candidate pipeline</h3><p className="mt-1 text-xs text-[#819088]">Anonymized profiles ranked against this run&apos;s criteria.</p></div><button onClick={() => go('Candidates')} className="text-xs font-bold text-[#507663]">View all</button></div>
        <div className="mt-4 divide-y divide-[#edf1ed]">{(shortlist?.entries ?? []).slice(0, 4).map((c: Entry, i: number) => <div key={c.candidate_ref} className="flex items-center gap-3 py-4"><div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#e7ece8] text-xs font-bold text-[#779083]">{String(i + 1).padStart(2, '0')}</div><div className="min-w-0 flex-1"><p className="text-sm font-semibold">{c.candidate_ref}</p><p className="mt-1 truncate text-xs text-[#8a9890]">{c.criteria.filter((x) => x.score > 0).map((x) => x.criterion.replace(/_/g, ' ')).join(' · ') || c.rationale}</p></div><div className="text-right"><p className="text-lg font-semibold">{Math.round(c.score * 100)}</p><p className="text-[10px] text-[#8a9890]">match score</p></div></div>)}{!shortlist && <p className="py-6 text-xs text-[#8a9890]">{running ? 'Ranking will appear when the run completes.' : 'No shortlist yet.'}</p>}</div></section>
      <RuleCard roleTitle={roleTitle} runId={run.run_id} disabled={running} onAdded={onRuleAdded} />
    </div>
    <section className="rounded-xl border border-[#dfe7e1] bg-white p-6"><div className="flex items-center justify-between"><div><h3 className="font-semibold">Latest activity</h3><p className="mt-1 text-xs text-[#819088]">A traceable record of decisions.</p></div><button onClick={() => go('Audit trail')} className="text-xs font-bold text-[#507663]">View audit trail</button></div><Activity audit={audit} /></section></>
}

function CandidatesView({ rows, run, query, setQuery, identities, onReview }: any) {
  return <><PageHead eyebrow="Candidate workspace" title="Candidates" body="Review anonymized applications, match evidence, and human decisions for the selected analysis run." />
    <div className="rounded-xl border border-[#dfe7e1] bg-white p-6"><div className="flex flex-col justify-between gap-4 md:flex-row md:items-center"><div><h3 className="font-semibold">{run?.screened ?? 0} candidates screened</h3><p className="mt-1 text-xs text-[#819088]">Identities remain hidden until you open a candidate for review.</p></div><div className="flex gap-2"><div className="flex items-center gap-2 rounded-lg bg-[#f5f8f5] px-3 py-2.5"><Search size={16} className="text-[#91a099]" /><input value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search candidates" placeholder="Search candidates" className="w-48 bg-transparent text-xs outline-none" /></div><button className="rounded-lg border border-[#dfe7e1] px-3 text-xs font-semibold"><Filter size={14} /></button></div></div>
      <div className="mt-6 overflow-x-auto"><table className="w-full min-w-[700px] text-left"><thead><tr className="border-b border-[#edf1ed] text-[10px] uppercase tracking-[0.12em] text-[#8a9890]"><th className="pb-3">Candidate</th><th className="pb-3">Match score</th><th className="pb-3">Evidence</th><th className="pb-3">Review state</th><th className="pb-3">Action</th></tr></thead>
        <tbody>{rows.map((r: Row, i: number) => { const identity = identities[r.ref]; return <tr key={r.ref} className="border-b border-[#edf1ed] last:border-0"><td className="py-4"><div className="flex items-center gap-3"><div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#e7ece8] text-xs font-bold text-[#779083]">{String(i + 1).padStart(2, '0')}</div><div><p className="text-sm font-semibold">{identity?.full_name ?? r.ref}</p><p className="text-xs text-[#8a9890]">{identity ? r.ref : 'anonymized'}</p></div></div></td><td className="py-4">{r.score !== null && <span className="text-lg font-semibold">{Math.round(r.score * 100)}</span>}<span className={`ml-2 rounded-full px-2 py-1 text-[10px] font-semibold ${toneClass[r.tone]}`}>{r.status}</span></td><td className="max-w-md py-4 text-xs text-[#718078]">{r.evidence}</td><td className="py-4 text-xs text-[#718078]">{identity !== undefined ? 'In review' : 'Not reviewed'}</td><td className="py-4"><button onClick={() => onReview(r.ref)} className="rounded-md border border-[#dfe7e1] px-3 py-2 text-xs font-semibold">{identity !== undefined ? 'Reviewing' : 'Review'}</button></td></tr> })}{rows.length === 0 && <tr><td colSpan={5} className="py-8 text-center text-xs text-[#8a9890]">No candidates to show yet.</td></tr>}</tbody></table></div></div></>
}

// The rule checker: plain language in, the legal finding and the compiled
// clause out — and, when the rule is lawful, onto the selected run with one
// click. A high-risk rule cannot be added; the rewrite is offered instead.
function RuleCard({ roleTitle, runId, disabled, onAdded }: { roleTitle: string; runId: string | null; disabled?: boolean; onAdded: () => void }) {
  const [rule, setRule] = useState('3+ years of experience at a leading company')
  const [result, setResult] = useState<Rule | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const check = async () => {
    setBusy(true); setNotice(null)
    try { const { rules } = await api.checkRules([rule], roleTitle === '—' ? undefined : roleTitle); setResult(rules[0] ?? null) } finally { setBusy(false) }
  }
  const add = async () => {
    if (!runId) return
    setBusy(true); setNotice(null)
    try {
      const added = await api.addRule(runId, rule)
      setNotice(`Added${added.rule.dsl ? ` as ${added.rule.dsl}` : ' for the human reviewer'}. Re-screening every candidate…`)
      setResult(null)
      onAdded()
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && (e.detail as any)?.rule) setResult((e.detail as any).rule as Rule)
      else setNotice(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }
  const useRewrite = () => { const rewrite = result?.findings[0]?.suggested_rewrite; if (rewrite) { setRule(rewrite); setResult(null) } }
  const canAdd = !!runId && !disabled && !!result && result.verdict !== 'risky'
  return <section className="rounded-xl border border-[#dfe7e1] bg-[#19312b] p-6 text-[#f1f6f0]"><div className="flex items-center gap-2"><WandSparkles size={17} className="text-[#f3b078]" /><h3 className="font-semibold">Rule checker</h3></div><p className="mt-2 text-xs leading-5 text-[#b2c4b8]">Write a hiring rule in plain language. Lawful rules can be added to this run; risky proxies are flagged with a rewrite and never applied.</p><textarea value={rule} onChange={(e) => { setRule(e.target.value); setResult(null); setNotice(null) }} className="mt-5 min-h-[90px] w-full resize-none rounded-lg border border-[#476458] bg-[#27463b] p-3 text-sm leading-6 text-white outline-none" />
    <div className="mt-3 flex gap-2"><button onClick={check} disabled={busy} className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-[#e7f1e7] py-2.5 text-xs font-bold text-[#19312b] disabled:opacity-60"><ShieldCheck size={15} /> {busy ? 'Working…' : 'Check this rule'}</button>{canAdd && <button onClick={add} disabled={busy} className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-[#f3b078] py-2.5 text-xs font-bold text-[#19312b] disabled:opacity-60"><Plus size={15} /> Add to this run</button>}</div>
    {notice && <p className="mt-3 rounded-lg bg-[#27463b] p-3 text-[11px] leading-5 text-[#cfe3d6]">{notice}</p>}
    {result && <RuleResult rule={result} onClose={() => setResult(null)} onUseRewrite={result.findings[0]?.suggested_rewrite ? useRewrite : undefined} />}</section>
}

function RuleResult({ rule, onClose, onUseRewrite }: { rule: Rule; onClose: () => void; onUseRewrite?: () => void }) {
  const finding = rule.findings[0]
  if (finding) return <div className="mt-4 rounded-lg border border-[#a85f45] bg-[#603e35] p-3"><div className="flex items-start gap-2"><Info size={16} className="mt-0.5 shrink-0 text-[#f4bd8d]" /><div className="min-w-0 flex-1"><p className="text-xs font-bold text-[#ffe0bd]">{rule.risk === 'high' ? 'Protected attribute or proxy — cannot be added' : 'Potential proxy — added only with a job-based justification'}</p><p className="mt-1 text-[11px] leading-5 text-[#f0cbb4]">{finding.explanation}</p><p className="mt-2 text-[11px] leading-5 text-[#f0cbb4]"><span className="font-bold">Recommended instead:</span> {finding.suggested_rewrite}</p><p className="mt-2 text-[10px] text-[#d9a58f]">{finding.statutes[0]}</p>{rule.dsl && <p className="mt-2 font-mono text-[10px] text-[#f4d9c8]">measurable part: {rule.dsl}</p>}{onUseRewrite && <button onClick={onUseRewrite} className="mt-3 rounded-md bg-[#f4bd8d] px-3 py-1.5 text-[11px] font-bold text-[#3b2620]">Use the rewrite</button>}</div><button onClick={onClose} className="ml-auto text-[#efc7b3]"><X size={14} /></button></div></div>
  if (rule.dsl) return <div className="mt-4 rounded-lg border border-[#4d7b62] bg-[#25473a] p-3"><div className="flex items-start gap-2"><Check size={16} className="mt-0.5 shrink-0 text-[#a8e0bd]" /><div><p className="text-xs font-bold text-[#d8f2e2]">Validated — tests a capability</p><p className="mt-1 font-mono text-[11px] leading-5 text-[#bfe3cd]">{rule.dsl}</p></div><button onClick={onClose} className="ml-auto text-[#bfe3cd]"><X size={14} /></button></div></div>
  return <div className="mt-4 rounded-lg border border-[#6d7f76] bg-[#2b3f37] p-3"><div className="flex items-start gap-2"><Info size={16} className="mt-0.5 shrink-0 text-[#cfe0d6]" /><div><p className="text-xs font-bold text-[#e4efe8]">Lawful, but a human has to judge it</p><p className="mt-1 text-[11px] leading-5 text-[#c5d6cc]">{rule.notes[0]}</p></div><button onClick={onClose} className="ml-auto text-[#cfe0d6]"><X size={14} /></button></div></div>
}

// Analysis runs: pick a run, see and change its rule set.
function RunsView({ runs, runId, onSelect, run, rules, running, roleTitle, onChanged, onNew }: any) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<{ tone: 'good' | 'warn' | 'bad'; title: string; body: string; rewrite?: string } | null>(null)
  const [search, setSearch] = useState('')
  const shown = runs.filter((r: RunSummary) => `${r.name ?? ''} ${r.role_title ?? ''} ${r.batch_id} ${r.id}`.toLowerCase().includes(search.toLowerCase()))

  const add = async () => {
    if (!runId || !text.trim()) return
    setBusy(true); setOutcome(null)
    try {
      const added = await api.addRule(runId, text.trim())
      const r = added.rule
      setOutcome(r.dsl
        ? { tone: r.risk === 'review' ? 'warn' : 'good', title: r.risk === 'review' ? 'Added — record the job-based justification' : 'Added and applied', body: r.dsl }
        : { tone: 'warn', title: 'Added for the human reviewer', body: r.notes[0] ?? 'No structured field tests this rule.' })
      setText(''); onChanged()
    } catch (e) {
      const rule = e instanceof ApiError && e.status === 422 ? (e.detail as any)?.rule as Rule | undefined : undefined
      const f = rule?.findings[0]
      setOutcome(f
        ? { tone: 'bad', title: 'Not added — screens on a protected attribute or a proxy', body: `${f.explanation} (${f.statutes[0] ?? ''})`, rewrite: f.suggested_rewrite }
        : { tone: 'bad', title: 'Could not add the rule', body: e instanceof Error ? e.message : String(e) })
    } finally { setBusy(false) }
  }
  const remove = async (ruleId: string) => {
    if (!runId) return
    setBusy(true)
    try { await api.removeRule(runId, ruleId); setOutcome({ tone: 'warn', title: 'Removed', body: 'Re-screening every candidate without it…' }); onChanged() } finally { setBusy(false) }
  }
  const verdictLabel = (r: Rule): [string, string] => r.verdict === 'applicable'
    ? (r.kind === 'prefer' ? ['Preference', 'bg-[#e8f1e9] text-[#47765f]'] : r.risk === 'review' ? ['Filter · justify', 'bg-[#fff1df] text-[#bd783d]'] : ['Filter', 'bg-[#e8f1e9] text-[#47765f]'])
    : r.verdict === 'risky' ? ['Flagged · not applied', 'bg-[#fbe5e1] text-[#a4453a]'] : ['Human review', 'bg-[#eef1ef] text-[#6f7e76]']

  return <><PageHead eyebrow="Analysis" title="Analysis runs" body="Each run is one rule set applied to one batch. Add or remove filters here; every addition is checked under anti-discrimination law before it touches a candidate." action={<button onClick={onNew} className="flex w-fit items-center gap-2 rounded-lg bg-[#19312b] px-4 py-2.5 text-sm font-semibold text-white"><Plus size={17} /> New analysis run</button>} />
    <div className="grid gap-6 xl:grid-cols-[.85fr_1.15fr]">
      <section className="rounded-xl border border-[#dfe7e1] bg-white p-6"><h3 className="font-semibold">Runs</h3>
        <div className="mt-3 flex items-center gap-2 rounded-lg bg-[#f5f8f5] px-3 py-2"><Search size={14} className="text-[#91a099]" /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by name, role or batch" className="w-full bg-transparent text-xs outline-none" /></div>
        <div className="mt-3 divide-y divide-[#edf1ed]">{shown.map((r: RunSummary) => <button key={r.id} onClick={() => onSelect(r.id)} className={`w-full py-3 text-left ${r.id === runId ? 'font-semibold' : ''}`}>
          <span className="flex items-center justify-between text-sm">{runLabel(r)}<span className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${statusClass(r.status)}`}>{r.status}</span></span>
          <span className="mt-0.5 block text-[11px] text-[#8a9890]">{r.role_title ?? 'no role'} · batch {r.batch_id} · {timeAgo(r.created_at)}</span>
        </button>)}{shown.length === 0 && <p className="py-4 text-xs text-[#8a9890]">No runs match.</p>}</div></section>

      <section className="rounded-xl border border-[#dfe7e1] bg-white p-6">
        <div><h3 className="font-semibold">{run ? `${roleTitle} / ${run.name || run.run_id}` : 'Select a run'}</h3>
          <p className="mt-1 text-xs text-[#819088]">{running ? `Re-screening… ${run?.screened ?? 0} of ${run?.total ?? 0}` : rules ? `${rules.applied} applied, ${rules.flagged} flagged, ${rules.rules.length} in total · batch ${run?.batch_id}` : 'No rules for this run yet.'}</p></div>
        <div className="mt-5 space-y-2">{(rules?.rules ?? []).map((r: Rule) => { const [label, cls] = verdictLabel(r); return <div key={r.id} className="flex items-start gap-3 rounded-lg border border-[#edf1ed] p-3"><div className="min-w-0 flex-1"><p className="text-sm">{r.source_text}</p>{r.dsl && <p className="mt-1 font-mono text-[11px] text-[#507663]">{r.dsl}</p>}{r.verdict === 'risky' && r.findings[0] && <p className="mt-1 text-[11px] text-[#a4453a]">{r.findings[0].statutes[0]} — try: {r.findings[0].suggested_rewrite}</p>}{r.verdict === 'unmappable' && <p className="mt-1 text-[11px] text-[#86938b]">{r.notes[0]}</p>}</div><span className={`whitespace-nowrap rounded-full px-2 py-1 text-[10px] font-bold ${cls}`}>{label}</span>{runId && <button disabled={busy || running} title="Remove and re-screen" onClick={() => remove(r.id)} className="rounded-md border border-[#dfe7e1] p-1 text-[#8a9890] hover:text-[#a4453a] disabled:opacity-40"><Trash2 size={13} /></button>}</div> })}</div>

        <div className="mt-5 rounded-lg bg-[#f5f8f5] p-4"><p className="text-xs font-semibold">Add a filter to this run</p><p className="mt-1 text-[11px] text-[#819088]">Plain language. Lawful rules are compiled and applied; a rule that screens on a protected attribute is refused and a rewrite recommended.</p>
          <div className="mt-3 flex gap-2"><input value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') add() }} placeholder="e.g. At least 3 years with Python" className="flex-1 rounded-lg border border-[#dfe7e1] bg-white px-3 py-2 text-sm outline-none focus:border-[#76a383]" /><button onClick={add} disabled={busy || running || !runId || !text.trim()} className="rounded-lg bg-[#19312b] px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">{busy ? 'Working…' : 'Add'}</button></div>
          {outcome && <div className={`mt-3 rounded-lg p-3 text-xs ${outcome.tone === 'good' ? 'bg-[#e8f1e9] text-[#2f5a43]' : outcome.tone === 'warn' ? 'bg-[#fff1df] text-[#8a5a2b]' : 'bg-[#fbe5e1] text-[#7a3a30]'}`}><p className="font-bold">{outcome.title}</p><p className="mt-1 leading-5">{outcome.body}</p>{outcome.rewrite && <button onClick={() => { setText(outcome.rewrite!); setOutcome(null) }} className="mt-2 rounded-md bg-white/70 px-3 py-1.5 text-[11px] font-bold">Use the recommended rewrite: “{outcome.rewrite}”</button>}</div>}</div>

        {rules?.reasoning && <div className="mt-5 border-t border-[#edf1ed] pt-4"><p className="text-xs font-semibold">How the plan was read</p><p className="mt-2 whitespace-pre-wrap text-xs leading-6 text-[#5d6e66]">{rules.reasoning}</p></div>}
      </section></div></>
}

// Batches: only resumes. No rules here.
function BatchesView({ batches, onSelectRun, onNew, onRefresh }: { batches: BatchSummary[]; onSelectRun: (runId: string) => void; onNew: () => void; onRefresh: () => void }) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const [detail, setDetail] = useState<Record<string, BatchDetail>>({})
  const open = async (id: string) => {
    setExpanded((current) => (current === id ? null : id))
    if (!detail[id]) {
      const body = await api.batch(id)
      setDetail((current) => ({ ...current, [id]: body }))
    }
  }
  const stages = ['pending', 'extracting', 'structuring', 'anonymizing', 'ready', 'needs_manual_review', 'failed', 'duplicate']
  return <><PageHead eyebrow="Storage" title="Batches of resumes" body="A batch is ingested once: extracted, structured and de-identified. Analyse it as many times as you like — the documents are never processed twice." action={<div className="flex gap-2"><button onClick={onRefresh} className="rounded-lg border border-[#dfe7e1] px-3 py-2.5 text-xs font-semibold">Refresh</button><button onClick={onNew} className="flex w-fit items-center gap-2 rounded-lg bg-[#19312b] px-4 py-2.5 text-sm font-semibold text-white"><Upload size={17} /> New batch</button></div>} />
    <div className="space-y-3">{batches.map((b) => { const d = detail[b.id]; return <section key={b.id} className="rounded-xl border border-[#dfe7e1] bg-white p-5">
      <button onClick={() => open(b.id)} className="flex w-full items-start justify-between gap-4 text-left">
        <div><p className="text-sm font-semibold">{b.name || b.id}</p><p className="mt-1 text-xs text-[#8a9890]">{b.id} · {b.total} documents · {b.ready} ready · {b.runs} {b.runs === 1 ? 'run' : 'runs'} · {timeAgo(b.created_at)}</p></div>
        <div className="flex items-center gap-2"><span className={`whitespace-nowrap rounded-full px-2 py-1 text-[10px] font-bold ${statusClass(b.status)}`}>{b.status}</span><ChevronDown size={16} className={`text-[#9aa59e] transition ${expanded === b.id ? 'rotate-180' : ''}`} /></div>
      </button>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[#edf1ed]"><div className="h-1.5 rounded-full bg-[#70a781]" style={{ width: `${b.total ? Math.round((b.ready / b.total) * 100) : 0}%` }} /></div>
      {expanded === b.id && <div className="mt-4 border-t border-[#edf1ed] pt-4">
        {!d && <p className="text-xs text-[#8a9890]">Loading…</p>}
        {d && <div className="grid gap-4 md:grid-cols-2">
          <div><p className="text-xs font-semibold">Documents</p><div className="mt-2 space-y-1">{stages.filter((s) => (d.counts[s] ?? 0) > 0).map((s) => <div key={s} className="flex justify-between text-xs text-[#718078]"><span>{s.replace(/_/g, ' ')}</span><span className="font-semibold">{d.counts[s]}</span></div>)}</div>{d.source?.prefix && <p className="mt-3 font-mono text-[10px] text-[#9aa59e]">{d.source.prefix}</p>}</div>
          <div><p className="text-xs font-semibold">Analysis runs</p><div className="mt-2 space-y-1">{d.runs.map((r) => <button key={r.id} onClick={() => onSelectRun(r.id)} className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs hover:bg-[#f0f4f0]"><span>{runLabel(r)}</span><span className={`rounded-full px-2 py-0.5 text-[10px] ${statusClass(r.status)}`}>{r.status}</span></button>)}{d.runs.length === 0 && <p className="text-xs text-[#8a9890]">None yet.</p>}</div></div>
        </div>}
      </div>}
    </section> })}{batches.length === 0 && <div className="rounded-xl border border-[#dfe7e1] bg-white p-8 text-center text-sm text-[#819088]">No batches yet. Add one to begin.</div>}</div></>
}

function AuditView({ audit, rules, run, reviewed, roleTitle }: any) {
  const [filter, setFilter] = useState<string>('all')
  const events = Array.from(new Set(audit.map((e: AuditEntry) => e.event))).sort() as string[]
  const shown = [...audit].reverse().filter((e: AuditEntry) => filter === 'all' || e.event === filter).slice(0, 80)
  return <><PageHead eyebrow="Traceability" title="Audit trail" body="Every rule, score and human decision in this analysis run — and the ingestion of the batch behind it." />
    <div className="grid gap-6 xl:grid-cols-[1.2fr_.8fr]"><section className="rounded-xl border border-[#dfe7e1] bg-white p-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-semibold">Decision history</h3><p className="mt-1 text-xs text-[#819088]">{audit.length} events for {roleTitle} / {run?.name || run?.run_id || '—'}.</p></div><label className="flex items-center gap-2 rounded-lg border border-[#dfe7e1] px-3 py-2 text-xs font-semibold"><Filter size={14} /><select value={filter} onChange={(e) => setFilter(e.target.value)} className="bg-transparent outline-none"><option value="all">All events</option>{events.map((ev) => <option key={ev} value={ev}>{ev.replace(/_/g, ' ')}</option>)}</select></label></div>
      <div className="mt-6 space-y-6">{shown.map((entry: AuditEntry, i: number) => { const { title, desc } = describeEvent(entry); return <div key={entry.id} className="flex gap-4"><div className="flex flex-col items-center"><div className={`flex h-9 w-9 items-center justify-center rounded-full ${entry.run_id ? 'bg-[#e8f1e9] text-[#47765f]' : 'bg-[#eef1ef] text-[#6f7e76]'}`}><FileCheck2 size={16} /></div>{i < shown.length - 1 && <div className="mt-2 h-full w-px bg-[#e2ebe3]" />}</div><div className="pb-2"><p className="text-xs text-[#8a9890]">{timeAgo(entry.at)} · {entry.stage}{entry.run_id ? '' : ' · batch'}</p><p className="mt-1 text-sm font-semibold">{title}</p><p className="mt-1 text-xs leading-5 text-[#718078]">{desc}</p>{entry.detail?.candidate_ref && <p className="mt-2 text-[10px] font-semibold uppercase tracking-wider text-[#a0aba4]">{entry.detail.candidate_ref}</p>}</div></div> })}{shown.length === 0 && <p className="text-xs text-[#8a9890]">No events yet.</p>}</div></section>
      <section className="rounded-xl border border-[#dfe7e1] bg-[#f1f6f1] p-6"><div className="flex items-center gap-2"><LockKeyhole size={17} /><h3 className="font-semibold">Explainability record</h3></div><p className="mt-3 text-sm leading-6 text-[#66786d]">Every score links back to the evidence and validated rule that produced it. Identity data is unlocked only after a human review decision.</p><div className="mt-6 space-y-3">{[['Rules compiled', String(rules?.rules.length ?? 0)], ['Rules flagged', String(rules?.flagged ?? 0)], ['Candidates screened', String(run?.screened ?? 0)], ['Human reviews', String(reviewed)], ['Batch', run?.batch_id ?? '—']].map(([a, b]) => <div className="flex justify-between border-b border-[#dfe7e1] pb-3 text-xs" key={a}><span className="text-[#819088]">{a}</span><span className="font-semibold">{b}</span></div>)}</div></section></div></>
}

function Activity({ audit }: { audit: AuditEntry[] }) {
  const icons: Record<string, typeof Layers3> = { plan_compiled: ShieldCheck, rule_risk_flagged: ShieldCheck, run_complete: FileCheck2, scored: Layers3, bucket_pulled: Upload, batch_created: Upload, run_created: Sparkles }
  const interesting = [...audit].reverse().filter((e) => ['batch_created', 'bucket_pulled', 'batch_complete', 'run_created', 'plan_compiled', 'rules_copied', 'rule_risk_flagged', 'rule_added', 'rescreen_complete', 'ensemble_started', 'run_complete', 'query_run'].includes(e.event)).slice(0, 5)
  return <div className="mt-4 space-y-4">{interesting.map((entry) => { const { title, desc } = describeEvent(entry); const Icon = icons[entry.event] ?? ClipboardCheck; return <div className="flex gap-3" key={entry.id}><div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[#edf4ee] text-[#5d8b6a]"><Icon size={15} /></div><div className="min-w-0 flex-1"><p className="text-xs font-semibold">{title}</p><p className="mt-0.5 truncate text-xs text-[#86938b]">{desc}</p></div><span className="whitespace-nowrap text-[10px] text-[#a0aba4]">{timeAgo(entry.at)}</span></div> })}{interesting.length === 0 && <p className="text-xs text-[#8a9890]">Nothing yet.</p>}</div>
}

// A batch: resumes only. No role, no plan, no rules.
function NewBatchModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState('')
  const [source, setSource] = useState<'bucket' | 'upload'>('bucket')
  const [bucketId, setBucketId] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const create = async () => {
    setBusy(true); setProblem(null)
    try {
      if (source === 'bucket') await api.batchFromBucket(bucketId.trim(), name.trim() || undefined)
      else await api.uploadBatch(files, name.trim() || undefined)
      onCreated()
    } catch (e) {
      setProblem(e instanceof ApiError ? (typeof e.detail === 'string' ? e.detail : (e.detail as any)?.message ?? e.message) : String(e))
    } finally { setBusy(false) }
  }
  return <Modal title="New batch" eyebrow="Storage" body="Add resumes. They are extracted and de-identified once; you choose the rules later, on an analysis run." onClose={onClose}>
    <label className="block text-xs font-semibold">Name (optional)<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Graduate intake, October" className="mt-2 w-full rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 text-sm outline-none focus:border-[#76a383]" /></label>
    <div className="flex gap-2 text-xs font-semibold">{(['bucket', 'upload'] as const).map((s) => <button key={s} onClick={() => setSource(s)} className={`rounded-lg border px-3 py-2 ${source === s ? 'border-[#19312b] bg-[#e8f0ea]' : 'border-[#dfe7e1] bg-white'}`}>{s === 'bucket' ? 'Already in storage' : 'Upload resumes'}</button>)}</div>
    {source === 'bucket'
      ? <label className="block text-xs font-semibold">Folder in storage (jobs/&lt;id&gt;/)<input value={bucketId} onChange={(e) => setBucketId(e.target.value)} placeholder="demo" className="mt-2 w-full rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 font-mono text-sm outline-none focus:border-[#76a383]" /></label>
      : <label className="block text-xs font-semibold">Resumes (PDF, DOCX, TXT or a zip)<input type="file" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))} className="mt-2 block w-full text-xs" /></label>}
    <div className="flex items-start gap-3 rounded-lg bg-[#f1f6f1] p-3 text-xs leading-5 text-[#66786d]"><LockKeyhole size={16} className="mt-0.5 shrink-0" />Names, contact details and institutions are removed during ingestion, before any rule sees a profile.</div>
    {problem && <p className="rounded-lg bg-[#fdf1ec] p-3 text-xs text-[#8a4a3d]">{problem}</p>}
    <ModalActions onClose={onClose} onSubmit={create} disabled={busy || (source === 'upload' ? files.length === 0 : !bucketId.trim())} label={busy ? 'Adding…' : 'Add batch'} />
  </Modal>
}

// A run: a batch plus a rule set.
function NewRunModal({ batches, runs, onClose, onCreated }: { batches: BatchSummary[]; runs: RunSummary[]; onClose: () => void; onCreated: (runId: string) => void }) {
  const ready = batches.filter((b) => b.status === 'complete' && b.ready > 0)
  const [batchId, setBatchId] = useState(ready[0]?.id ?? '')
  const [role, setRole] = useState('Senior Data Engineer')
  const [name, setName] = useState('')
  const [rulesFrom, setRulesFrom] = useState('')
  const [plan, setPlan] = useState('Must have 5+ years experience, Python and SQL, plus AWS or GCP. Bachelor degree or higher. Nice to have: Terraform. Should have led an on-call rotation.')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const create = async () => {
    setBusy(true); setProblem(null)
    try {
      const created = await api.createRun(batchId, { title: role }, { name: name.trim() || undefined, plan, rulesFrom: rulesFrom || undefined })
      onCreated(created.run_id)
    } catch (e) {
      setProblem(e instanceof ApiError ? (typeof e.detail === 'string' ? e.detail : (e.detail as any)?.message ?? e.message) : String(e))
    } finally { setBusy(false) }
  }
  return <Modal title="New analysis run" eyebrow="Analysis" body="Apply a rule set to a batch of resumes. Requirements that screen on a protected attribute are flagged, never applied." onClose={onClose}>
    <label className="block text-xs font-semibold">Batch<select value={batchId} onChange={(e) => setBatchId(e.target.value)} className="mt-2 w-full rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 text-sm outline-none focus:border-[#76a383]">{ready.length === 0 && <option value="">No ingested batches yet</option>}{ready.map((b) => <option key={b.id} value={b.id}>{b.name || b.id} — {b.ready} candidates</option>)}</select></label>
    <label className="block text-xs font-semibold">Role<input value={role} onChange={(e) => setRole(e.target.value)} className="mt-2 w-full rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 text-sm outline-none focus:border-[#76a383]" /></label>
    <label className="block text-xs font-semibold">Name this run (optional)<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Strict screen" className="mt-2 w-full rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 text-sm outline-none focus:border-[#76a383]" /></label>
    <label className="block text-xs font-semibold">Rules<select value={rulesFrom} onChange={(e) => setRulesFrom(e.target.value)} className="mt-2 w-full rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 text-sm outline-none focus:border-[#76a383]"><option value="">Compile from the hiring plan below</option>{runs.filter((r) => r.status === 'complete').map((r) => <option key={r.id} value={r.id}>Reuse the rules from {runLabel(r)}</option>)}</select></label>
    {!rulesFrom && <label className="block text-xs font-semibold">Hiring plan<textarea value={plan} onChange={(e) => setPlan(e.target.value)} className="mt-2 min-h-[110px] w-full resize-none rounded-lg border border-[#dfe7e1] bg-white px-3 py-2.5 text-sm leading-6 outline-none focus:border-[#76a383]" /></label>}
    {problem && <p className="rounded-lg bg-[#fdf1ec] p-3 text-xs text-[#8a4a3d]">{problem}</p>}
    <ModalActions onClose={onClose} onSubmit={create} disabled={busy || !batchId || !role.trim()} label={busy ? 'Starting…' : 'Start analysis'} />
  </Modal>
}

function Modal({ title, eyebrow, body, onClose, children }: { title: string; eyebrow: string; body: string; onClose: () => void; children: React.ReactNode }) {
  return <div className="fixed inset-0 z-30 flex items-center justify-center bg-[#19312b]/35 p-4"><div role="dialog" aria-modal="true" className="w-full max-w-lg rounded-2xl border border-[#dfe7e1] bg-[#fbfcfa] p-6 shadow-2xl">
    <div className="flex items-start justify-between"><div><p className="text-xs font-bold uppercase tracking-[0.14em] text-[#6c8175]">{eyebrow}</p><h2 className="mt-2 text-2xl font-semibold">{title}</h2><p className="mt-2 text-sm leading-6 text-[#819088]">{body}</p></div><button onClick={onClose} aria-label="Close" className="rounded-full p-2 text-[#718078] hover:bg-[#eef3ee]"><X size={18} /></button></div>
    <div className="mt-6 space-y-4">{children}</div></div></div>
}

function ModalActions({ onClose, onSubmit, disabled, label }: { onClose: () => void; onSubmit: () => void; disabled: boolean; label: string }) {
  return <div className="flex justify-end gap-3 pt-2"><button onClick={onClose} className="rounded-lg border border-[#dfe7e1] px-4 py-2.5 text-xs font-semibold">Cancel</button><button onClick={onSubmit} disabled={disabled} className="rounded-lg bg-[#19312b] px-4 py-2.5 text-xs font-semibold text-white disabled:opacity-60">{label}</button></div>
}
