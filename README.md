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
bulk upload → dedup → Tika extraction (+OCR fallback) → structuring
  → anonymization (+leak check) → rule engine (+legal-risk classifier)
  → triage ranking → ensemble on borderline cases → shortlist + audit trail
  → human review (identity re-attached)
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

vLLM and SGLang both serve the OpenAI-compatible surface this expects, and both
do continuous batching behind it, which is what makes a bulk upload tractable.
The client negotiates schema-guided decoding (`json_schema`, then `guided_json`,
then plain JSON mode) and caches whichever the server accepted.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness and configured backend. Public. |
| `POST` | `/rules/check` | Review screening rules without running a batch. |
| `POST` | `/rules/check-one` | Review a single rule. |
| `POST` | `/jobs` | Bulk upload (files or zip). Returns a job id immediately. |
| `GET` | `/jobs/{id}/status` | Per-status counts. Poll this for a progress view. |
| `GET` | `/jobs/{id}/rules` | Classified rule set for the job. |
| `GET` | `/jobs/{id}/shortlist` | Shortlist, identity re-attached by default. |
| `GET` | `/jobs/{id}/candidates` | Per-candidate state; identity withheld by default. |
| `GET` | `/jobs/{id}/audit` | Full decision trail. |

Set `RESCAN_API_KEYS` (comma-separated) to require `Authorization: Bearer <key>`
or `X-API-Key`. **An empty value runs the API unauthenticated**; it logs a
warning at startup.

Identity is re-attached at `/jobs/{id}/shortlist` on purpose. Full anonymization
is right for the machine passes but backfires for human reviewers, so the people
doing the interviewing see names again.

## The rule engine

A recruiter's free-text rule takes one of three paths:

- **applied** — compiles to a structured test and filters candidates;
- **flagged** — reads as a proxy for a protected attribute, so it is reported
  with a statute basis and a measurable rewrite, and is *never* compiled into a
  filter even when it could be;
- **unmappable** — lawful but not mechanisable, passed to the human reviewer
  rather than used to exclude anyone.

```
$ curl -s localhost:8080/rules/check -H 'content-type: application/json' \
    -d '{"rules":["Must be a native English speaker"]}' | jq -r '.rules[0].findings[0] | .statutes[0], .suggested_rewrite'

Racial Discrimination Act 1975 (Cth) ss 9, 15 — race, colour, national or ethnic origin
Communicates in written and spoken English at a professional standard, evidenced in the application.
```

Sixteen known risky phrasings are matched deterministically (`rescan/rules/statutes.py`)
so the advice does not vary between runs; the model catches novel phrasing the
table misses. **The model can add risk but never remove it.**

Review-level rules — a citizenship requirement, for instance — are lawful with a
job-based justification, so they are applied but ask for that justification to be
recorded.

Qualification levels are assigned by a deterministic AQF table, never by the
model, so two candidates holding the same award always get the same level. An
unrecognised award means "check this by hand", not "reject".

Legal references cover the Racial Discrimination Act 1975, Fair Work Act 2009
s 351, Sex Discrimination Act 1984, Age Discrimination Act 2004, Disability
Discrimination Act 1992 and the Anti-Discrimination Act 1991 (Qld). This is
decision support for recruiters, not legal advice.

## MCP server

The rule engine is also an MCP server, so the wording check is available in any
MCP-capable client before a phrase ever becomes a screening rule.

```bash
python -m rescan.mcp_server           # stdio
python -m rescan.mcp_server --http    # streamable HTTP
```

Tools: `check_screening_rule`, `check_screening_rules`,
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

`docker-compose.yml` runs the API alongside Tika. Set `RESCAN_API_KEYS` and
point `RESCAN_LLM_BASE_URL` at the inference host.

## Status

Verified in development:

- 171 tests pass against the deterministic backend.
- Extraction verified end to end through a live Tika 2.9.2 server across txt,
  docx and pdf.
- Full bulk job verified: 14 documents → 12 processed, 1 duplicate skipped, 1
  corrupt document dead-lettered, 200 audit entries.
- The ensemble fires on a subset (3 of 9 on the sample batch), not the batch.

Not yet verified:

- **No run against a real served model.** No GPU was available, so every model
  pass has been exercised only through the deterministic backend. The prompts,
  guided-decoding schemas and JSON recovery are written but unexercised against
  real generations, and the bias audit has no real result yet. This is the
  first thing to do once inference is up.
- **The Docker build and compose stack are unbuilt** — Docker was not available.
- **OCR is untested end to end**; neither Tesseract nor poppler was installed, so
  the fallback was only verified to degrade correctly (warning recorded, document
  dead-lettered rather than dropped). The Dockerfile installs both.
- The loader for the published `nghiemhnlp/bias_resume_public` study data is
  written against the dataset's real schema, but its filter endpoint was still
  building an index throughout, so that path is unexercised. The synthetic
  corpus is the default and needs no network.
