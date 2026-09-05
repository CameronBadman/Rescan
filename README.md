# Rescan

Bias-aware applicant triage with an auditable rule engine. Backend only — this
service exposes an HTTP API and an MCP server for a separate frontend to drive.

The premise: entry-level hiring discrimination in Australia is substantially
*statistical* rather than taste-based — an information problem — and prompt-based
debiasing does not work. So the system removes the signal instead of asking a
model to ignore it, decides eligibility with deterministic rules rather than
scores, and writes down why for every candidate.

## Pipeline

```
recruiter's plan ──► compile (legal brief + reasoning) ──► rules in the query language
                                                                    │
bucket <prefix>/<jobId>/ or upload → dedup → Tika extraction (+OCR) → structuring
  → anonymization (+leak check) → rule engine (REQUIRE clauses) ◄────┘
  → triage ranking (PREFER clauses) → ensemble on borderline cases
  → shortlist + audit trail → human review (identity re-attached)
                                              ▲
ad-hoc queries in the same language ──────────┘  (POST /jobs/{id}/query)
```

Three design commitments run through it:

**Exclusions never come from a score.** Ranking only orders candidates who have
already cleared the rule set. Every exclusion is produced by the rule engine and
carries a plain-language reason naming the structured value it was decided on —
"Candidate has 3 years of professional experience; the rule requires at least 5"
— because a bare score is not a reason a person can be given or an employer can
defend.

**Silence is not evidence.** A value the resume never stated yields "could not
determine" and routes to manual review, never a rejection. A document that
defeats every extraction backend is retried and then dead-lettered — not
dropped, and not counted as rejected.

**De-identification does not depend on the model behaving.** The model rewrites
free text and tiers institutions, but a deterministic scrub strips identity
tokens from every field afterwards, and a leak check over the serialised profile
aborts the candidate rather than letting identity reach ranking.

## Quick start

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'

# Optional: Tika server. Without it, extraction falls back to pypdf/python-docx.
mkdir -p vendor && curl -L -o vendor/tika-server.jar \
  https://repo1.maven.org/maven2/org/apache/tika/tika-server-standard/2.9.2/tika-server-standard-2.9.2.jar
./scripts/tika_server.sh &

./.venv/bin/python -m pytest -q
./.venv/bin/uvicorn rescan.api.main:app --reload --port 8080
```

The default `stub` inference backend is a deterministic local implementation of
every model pass. It needs no GPU, and it is what the test suite runs against.
Point at the real model with:

```bash
export RESCAN_LLM_BACKEND=openai
export RESCAN_LLM_BASE_URL=http://gpu-host:8000/v1
export RESCAN_LLM_MODEL=Qwen/Qwen3.8-27B
```

### The model

The system is built for an open-weights model — no proprietary API in the
loop. The target is **[Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B)**
(Apache 2.0, 27B dense, 262k context): `scripts/serve_vllm.sh` starts it with
the flags that matter, and `docker-compose.yml` carries the same as a `vllm`
service. vLLM, SGLang and llama.cpp all expose the OpenAI-compatible surface
the client speaks; the first two do continuous batching behind it, which is
what makes a bulk upload tractable.

Two things the client handles for open reasoning models:

- **Thinking is off per request.** Qwen3.x thinks by default. Every pass here
  already carries its reasoning in its schema where it needs it (the compile
  pass writes `reasoning` before `rules`; the judge writes `reasoning` before
  `answer`), so the client sends `chat_template_kwargs: {"enable_thinking":
  false}` and strips any `<think>` block that arrives anyway before parsing.
  Leave thinking on with `RESCAN_LLM_DISABLE_THINKING=false` and set
  `RESCAN_LLM_REASONING_EFFORT=low|medium|xhigh` if a pass needs it; the vLLM
  script configures the `qwen3` reasoning parser so the thinking then lands in
  `reasoning_content`, not in the JSON.
- **Structured output is negotiated, not assumed.** The client probes
  `json_schema` (vLLM, SGLang, current llama.cpp), then llama.cpp's older
  `json_object`+`schema`, then vLLM's `guided_json`, then plain JSON mode, and
  remembers what the server accepted — including whether it tolerates the
  extra request fields. A rejection is recognised by its body as well as its
  status, because llama.cpp's server answers an unsupported style with a 500.

Once a server is up, run the smoke test before anything else. It sends each
pass one real request, validates the JSON against the pass's contract, reports
the negotiated style, latency and token counts, then runs one whole job:

```bash
RESCAN_LLM_BACKEND=openai RESCAN_LLM_BASE_URL=http://gpu-host:8000/v1 \
  python -m scripts.smoke_real_model
```

`GET /health/llm` reports the same reachability and negotiated style live.

**GPUs on RunPod.** `infra/runpod/` is a Terraform module that brings up one
vLLM pod per model — weights on a network volume, spot by default — and
prints the `.env` lines. Its default shape runs the per-candidate passes on
Qwen3.8-27B and routes the once-per-job plan-compile pass to
**Qwen3-235B-A22B-Instruct-2507-FP8** on four H100s (`RESCAN_COMPILE_*`; the
client sends `compile_dsl` and its repair round there and everything else to
the 27B). `terraform apply` for a batch, `terraform destroy` after. See
`infra/runpod/README.md`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness and configured backend. Public. |
| `POST` | `/rules/compile` | Compile a recruiter's whole plan into rules; returns the model's reasoning with them. |
| `POST` | `/rules/check` | Review discrete rules, one result per rule in order. |
| `POST` | `/rules/check-one` | Review a single rule. |
| `GET` | `/dsl/fields` | The language reference: grammar, fields, records, aggregates, forbidden identifiers. |
| `POST` | `/dsl/parse` | Validate a program or query; a forbidden field is a 422 with its statute. |
| `POST` | `/jobs` | Bulk upload (files or zip) with a `plan` and/or `rules`. Returns a job id immediately. |
| `POST` | `/jobs/from-bucket` | Start a job from `<prefix>/<jobId>/` in the object store. |
| `GET` | `/jobs/{id}/status` | Per-status counts. Poll this for a progress view. |
| `GET` | `/jobs/{id}/rules` | Compiled rule set, reasoning and source plan for the job. |
| `POST` | `/jobs/{id}/query` | Run a query over the job's anonymized profiles. |
| `GET` | `/jobs/{id}/shortlist` | Shortlist, identity re-attached by default. |
| `GET` | `/jobs/{id}/candidates` | Per-candidate state; identity withheld by default. |
| `GET` | `/jobs/{id}/audit` | Full decision trail. |

Set `RESCAN_API_KEYS` (comma-separated) to require `Authorization: Bearer <key>`
or `X-API-Key`. **An empty value runs the API unauthenticated**; it logs a
warning at startup.

Identity is re-attached at `/jobs/{id}/shortlist` on purpose. Full anonymization
is right for the machine passes but backfires for human reviewers, so the people
doing the interviewing see names again.

## The rule language

Rules are written in a small query language over the anonymized profile. The
model writes it from the recruiter's plan; the parser validates it; recruiters
read it in the audit trail. It is also the query base for the resumes — the same
language runs ad hoc over a processed job.

```
REQUIRE years_experience >= 5
REQUIRE skills HAS ALL ("Python", "SQL") AND skills HAS ANY ("AWS", "GCP", "Azure")
REQUIRE aqf >= 7 OR years_experience >= 8
REQUIRE work_rights IS unrestricted
REQUIRE ANY qualification WHERE field_of_study HAS ANY ("computer science", "engineering") AND aqf >= 7
REQUIRE COUNT(role WHERE seniority IN ("senior", "lead")) >= 1
REQUIRE MAX(skill.years WHERE name = "Python") >= 3
PREFER technologies HAS ANY ("Kafka", "Flink") WEIGHT 2 BECAUSE "Streaming is the core of the role."
REQUIRE ASK "Has the candidate led an on-call rotation or incident response?"
```

`REQUIRE` clauses screen: a candidate who fails one is excluded, with the reason
shown. `PREFER` clauses rank: they become the scoring criteria with their
declared `WEIGHT`, and a match score decomposes into them. The vocabulary is
large on purpose — seventy-odd named fields (durations, counts, category-filtered
skill lists, seniority, booleans such as `has_degree`), four record types
(`skill`, `role`, `qualification`, `project`) quantified with `ANY … WHERE` and
aggregated with `COUNT/SUM/MAX/MIN/AVG`, and `ASK "…"` for a yes/no question the
model answers from the profile. `GET /dsl/fields` lists all of it.

Four properties hold across the whole language:

**Three-valued.** A value the resume does not state is *unknown*, and unknown
never fails a candidate on its own: `UNKNOWN AND FALSE` is `FALSE`, `UNKNOWN OR
TRUE` is `TRUE`, anything else is unknown and goes to manual review. Empty lists
are unknown, not zero. An aggregate over records that could not be assessed
reports a range and stays undecided unless the answer is the same either way.

**Proxies cannot enter through a field name.** `region`, `institution`,
`completion_year`, `nationality`, `summary` and forty-odd others are *forbidden
identifiers*: a rule naming one fails at parse time with the statute it engages
and the alternative to write instead —
`'region' is not queryable: location is a proxy for race and social origin …
(Racial Discrimination Act 1975 (Cth) ss 9, 15 …)`. String literals and `ASK`
questions are scanned against the risky-phrase table too. Employer names are
*not* forbidden: they stay in the profile and are queryable (`ANY role WHERE
employer = "…"`); only an employer whose name reveals a protected attribute — a
party, a union, a religious body, an ethnic or advocacy group — is dropped. The
anonymization model makes that call (it already sees every employer, so it is a
field in that pass, not an extra one); the code applies it and records the
reason, and if the model says nothing every employer stays. Prestige phrasing in a rule ("leading company") is still
flagged for review.

**Every outcome is a sentence.** "Candidate has 3 years of professional
experience; the rule requires at least 5 years." An `AND` names only the
conjuncts that failed; an `OR` lists every alternative; `ANY` names the record
that satisfied it.

**Model checks are accountable.** An `ASK` "no" counts only when the model
quotes evidence that is actually in the profile; in a `REQUIRE` clause the
question goes to the ensemble and a split vote is unknown; the model may decline
when answering would mean inferring a protected attribute. Every check is in the
audit trail with question, answer, evidence and votes.

### From a plan to rules

`POST /rules/compile` (or `plan` on `POST /jobs`) hands the recruiter's whole
plan to the model with the legal brief in front of it — the statutes, the
indirect-discrimination doctrine, every phrasing the deterministic table flags
with its rewrite — and the language reference. The model reasons first (what
the role needs, which phrases are proxies and under which Act, how each was
rewritten, what is hard and what is a preference), then writes one clause per
requirement. Its reasoning is stored with the rule set.

The model can add risk but never remove it. Seventeen known risky phrasings are
matched deterministically (`rescan/rules/statutes.py`) over the recruiter's text
and over every literal in the compiled clause; a rule the table rates high risk
is reported with a statute and a rewrite and *never* applied, even when it
compiled cleanly. A clause that does not parse gets one repair round, then goes
to a human. If inference is down the statute table still runs and nothing is
applied.

```
$ curl -s localhost:8080/rules/compile -H 'content-type: application/json' -d '{
    "plan": "Must have 5+ years experience, Python and SQL, plus AWS or GCP. Bachelor degree or higher.
             Must be a native English speaker and a recent graduate from a leading company.
             Nice to have: Terraform. Should have led an on-call rotation."}' \
  | jq -r '.rules[] | "\(.verdict)\t\(.dsl // ("flagged: " + (.findings|map(.pattern_id)|join(", "))))"'

applicable  REQUIRE years_experience >= 5 AND skills HAS ALL ("Python", "SQL") AND skills HAS ANY ("AWS", "GCP")
applicable  REQUIRE aqf >= 7
risky       flagged: native_speaker, recent_graduate, employer_prestige
applicable  PREFER skills HAS ANY ("Terraform")
applicable  REQUIRE ASK "Has the candidate led an on-call rotation?"
```

Review-level rules — a citizenship requirement, or "leading company" — are
applied where a measurable part exists, with a note to record the job-based
justification.

Qualification levels are assigned by a deterministic AQF table, never by the
model, so two candidates holding the same award always get the same level. An
unrecognised award means "check this by hand", not "reject".

Legal references cover the Racial Discrimination Act 1975, Fair Work Act 2009
s 351, Sex Discrimination Act 1984, Age Discrimination Act 2004, Disability
Discrimination Act 1992, the Anti-Discrimination Act 1991 (Qld) and the Privacy
Act 1988. This is decision support for recruiters, not legal advice.

### Querying a job

```
$ curl -s localhost:8080/jobs/round-7/query -H 'content-type: application/json' \
    -d '{"dsl": "years_experience >= 5 AND ANY skill WHERE name = \"Python\""}' | jq '.counts, .matched[0]'
```

Returns matched, not matched and undecided candidates by reference with a reason
each, never identity. Queries pass the same legal gate as rules and are written
to the audit trail, because a query that never formally excludes anyone still
shapes who gets looked at.

## Where the resumes come from

Resumes for a hiring round live in an S3-compatible bucket under
`<prefix>/<jobId>/` — MinIO, Cloudflare R2 or AWS all work, through boto3 with
path-style addressing. `POST /jobs/from-bucket {"job_id": "round-7", "role":
{...}, "plan": "..."}` pulls that prefix, expands archives, and runs the job
under the bucket's own job id so the frontend needs no mapping. Non-documents
(a manifest, a thumbnail) are skipped and reported, not failed.

```bash
export RESCAN_OBJECT_STORE=s3
export RESCAN_S3_ENDPOINT_URL=http://minio:9000     # omit for AWS
export RESCAN_S3_BUCKET=resumes
export RESCAN_S3_ACCESS_KEY_ID=...
export RESCAN_S3_SECRET_ACCESS_KEY=...
export RESCAN_S3_PREFIX=jobs
```

`RESCAN_OBJECT_STORE=local` (the default) reads the same layout from
`data/bucket/`, the way the stub stands in for inference.

## MCP server

The rule engine is also an MCP server, so the wording check is available in any
MCP-capable client before a phrase ever becomes a screening rule.

```bash
python -m rescan.mcp_server           # stdio
python -m rescan.mcp_server --http    # streamable HTTP
```

Tools: `compile_hiring_plan`, `check_screening_rule`, `check_screening_rules`,
`describe_query_language`, `parse_query`, `query_candidates`,
`list_known_risky_phrases`, `map_qualification_to_aqf`.

## Bias audit

There is no published bias evaluation for the target model, so the audit harness
is the evidence. It scores counterfactual variants — byte-identical resumes
differing only in the applicant's name — with and without anonymization, and
reports the score gap across demographic groups in each arm.

```bash
RESCAN_LLM_BACKEND=openai RESCAN_LLM_BASE_URL=http://gpu-host:8000/v1 \
  python -m rescan.audit.run --source synthetic --proxies --out data/bias_audit.json
```

`--proxies` also varies institution and suburb, testing whether removing the
name alone is enough — the literature says it is not.

Two tests drive a deliberately name-biased scorer to prove the harness measures
what it claims: the identified arm reports the gap, the anonymized arm closes it.
Against the `stub` backend both arms are identical by construction, and the
report says so rather than presenting a zero gap as a finding.

## Deployment

`docker-compose.yml` runs the API alongside Tika and a MinIO bucket. Set
`RESCAN_API_KEYS` and point `RESCAN_LLM_BASE_URL` at the inference host.

Rule sets stored before the language existed carry a `predicate` key the
current model ignores; they load with no clause and are not applied. Re-run the
job to compile its rules.

## Status

Verified in development:

- 348 tests pass against the deterministic backend, including the language
  (parser, three-valued evaluation, aggregates, the judge), plan compilation,
  queries, bucket ingestion against moto's S3, and the inference client
  against scripted servers.
- **The real inference path was exercised against a real open model** —
  Qwen3-0.6B on a local llama.cpp server, CPU only. Structured-output
  negotiation lands on an enforcing style, thinking control is honoured, and
  structuring, anonymization, the judge and ranking each returned
  schema-valid JSON. Three defects that run surfaced are fixed: a near-miss
  enum value no longer fails a resume, a `maxLength` in a schema no longer
  crashes a grammar-based decoder, and model-written anonymization fields
  are scrubbed rather than aborting the candidate.
- Extraction verified end to end through a live Tika 2.9.2 server across txt,
  docx and pdf.
- Full bulk job verified: 14 documents → 12 processed, 1 duplicate skipped, 1
  corrupt document dead-lettered, 200 audit entries.
- The ensemble fires on a subset (3 of 9 on the sample batch), not the batch.

Not yet verified:

- **No run against Qwen3.8-27B.** No GPU was available. The 0.6B model used
  to exercise the client is far too small to judge quality: it could not keep
  the compile pass's reasoning within an 8k-token budget, so **plan
  compilation has not completed against any real model**, and how well the
  27B writes the rule language — and how often the repair round fires — is
  unmeasured. The bias audit has no real result yet. Run
  `python -m scripts.smoke_real_model` against the served 27B first.
- **No live S3 endpoint.** The boto3 path is exercised against moto only; the
  local directory store is what ran end to end.
- **The Docker build and compose stack are unbuilt** — Docker was not available.
- **OCR is untested end to end**; neither Tesseract nor poppler was installed, so
  the fallback was only verified to degrade correctly (warning recorded, document
  dead-lettered rather than dropped). The Dockerfile installs both.
- The loader for the published `nghiemhnlp/bias_resume_public` study data is
  written against the dataset's real schema, but its filter endpoint was still
  building an index throughout, so that path is unexercised. The synthetic
  corpus is the default and needs no network.
