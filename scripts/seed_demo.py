#!/usr/bin/env python3
"""Seed the database with a synthetic corpus: large batches and thousands of runs.

The demo needs scale that inference cannot produce in a hackathon slot — a
resume takes tens of seconds of GPU time, so a few thousand analysis runs over
a few thousand people is days of compute. This script fabricates that history
directly in the store.

What is synthetic and what is real is worth being precise about, because the
demo is about auditability:

* The **profiles** are generated, not extracted. No model is called, so no
  document is misrepresented as having been read.
* The **rules** are real: each one is parsed by `rescan.dsl` into the same
  clause tree a compiled recruiter plan produces.
* The **screening** is real: every outcome comes from `rescan.rules.engine`
  evaluating those clauses against the generated profiles, with the same
  three-valued logic, so unknown still routes to manual review rather than
  rejecting anyone.
* Only the **scores** are fabricated (deterministically, from the profile),
  since ranking is the one pass that has no non-model implementation.

Rows written here are marked `synthetic: true` in the batch source and in the
audit trail, so seeded history can never be mistaken for a real decision.

    python scripts/seed_demo.py                        # ~2,000 runs
    python scripts/seed_demo.py --runs 3000 --flagship 2500
    python scripts/seed_demo.py --db data/rescan.db --reset-synthetic
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from rescan.dsl.parser import parse_clause
from rescan.pipeline.rank import build_shortlist
from rescan.rules.engine import screen
from rescan.rules.models import ClassifiedRule, RuleSet, RuleVerdict
from rescan.schemas import (
    AnonymizedProfile,
    BatchStatus,
    CandidateOutcome,
    CandidateScore,
    CandidateStatus,
    CriterionScore,
    Experience,
    Qualification,
    RoleSpec,
    RunStatus,
    Skill,
    WorkRights,
    WorkRightsStatus,
)
from rescan.store import Store

# ---------------------------------------------------------------- vocabulary

TECH = ["Python", "TypeScript", "Java", "Go", "Kubernetes", "AWS", "Terraform", "React",
        "PostgreSQL", "Kafka", "Spark", "dbt", "Snowflake", "Rust", "C#", "Swift",
        "Django", "FastAPI", "Node.js", "GraphQL", "Docker", "Airflow", "PyTorch"]
DOMAIN = ["clinical governance", "payments", "underwriting", "supply chain", "mining safety",
          "public sector procurement", "telehealth", "logistics", "retail analytics"]
TOOLS = ["Jira", "Figma", "Tableau", "Power BI", "ServiceNow", "Salesforce", "SAP"]
EMPLOYERS = ["Suncorp", "Telstra", "Queensland Health", "Canva", "Atlassian", "Bunnings",
             "NAB", "Woolworths Group", "CSIRO", "Aurizon", "Domain Group", "Xero",
             "Brisbane City Council", "Deloitte", "REA Group", "Flight Centre"]
INDUSTRIES = ["banking", "health", "SaaS", "retail", "government", "mining", "logistics", "telco"]
TITLES = ["Software Engineer", "Senior Software Engineer", "Data Engineer", "Platform Engineer",
          "Business Analyst", "Registered Nurse", "Project Coordinator", "Site Reliability Engineer",
          "Product Manager", "Solutions Architect", "Support Engineer", "Data Analyst"]
SENIORITY = ["graduate", "junior", "mid", "senior", "lead", "principal", "manager"]
QUALS = [("Bachelor of Information Technology", "information technology", 7, "Bachelor Degree"),
         ("Bachelor of Nursing", "nursing", 7, "Bachelor Degree"),
         ("Master of Data Science", "data science", 9, "Masters Degree"),
         ("Diploma of Project Management", "project management", 5, "Diploma"),
         ("Certificate IV in Cyber Security", "cyber security", 4, "Certificate IV"),
         ("Bachelor of Commerce", "accounting", 7, "Bachelor Degree")]
TIERS = ["Australian university", "Australian university (Group of Eight)",
         "Australian TAFE", "Overseas university (recognised)"]
REGIONS = ["Greater Brisbane", "Gold Coast", "Sunshine Coast", "Regional Queensland",
           "Greater Sydney", "Greater Melbourne", "Remote (Australia)"]
CERTS = ["AWS Certified Solutions Architect", "Certified Scrum Master", "ITIL v4 Foundation",
         "AHPRA registration", "PRINCE2 Practitioner", "CISSP"]
LICENCES = ["Driver's licence (C)", "Blue Card", "White Card", "RN registration"]

ROLE_TITLES = ["Senior Backend Engineer", "Data Engineer", "Registered Nurse (Ward)",
               "Platform Engineer", "Business Analyst", "Site Reliability Engineer",
               "Graduate Software Engineer", "Product Manager", "Cyber Security Analyst",
               "Clinical Nurse Consultant", "Solutions Architect", "Support Engineer"]

# Rules a recruiter could lawfully ask for, each already in the rule language.
RULE_BANK: list[tuple[str, str, str]] = [
    ("require", "At least 3 years of professional Python experience.",
     'REQUIRE MAX(skill.years WHERE name = "Python") >= 3 BECAUSE "The role maintains a Python service."'),
    ("require", "Must hold an unrestricted right to work in Australia.",
     'REQUIRE work_rights_unrestricted IS TRUE BECAUSE "The role cannot be sponsored."'),
    ("require", "At least 5 years of total professional experience.",
     'REQUIRE years_experience >= 5 BECAUSE "The role is a senior individual contributor."'),
    ("require", "A bachelor degree or higher.",
     'REQUIRE aqf >= 7 BECAUSE "The role requires accredited study."'),
    ("require", "Has worked with Kubernetes.",
     'REQUIRE skills HAS ANY ("Kubernetes") BECAUSE "The platform runs on Kubernetes."'),
    ("require", "Has led a team of at least four people.",
     'REQUIRE people_managed_max >= 4 BECAUSE "The role leads a squad."'),
    ("require", "Available within eight weeks.",
     'REQUIRE availability_weeks <= 8 BECAUSE "The project starts next quarter."'),
    ("require", "Has held a role at senior level or above.",
     'REQUIRE ANY role WHERE seniority IN ("senior", "lead", "principal") BECAUSE "The role is senior."'),
    ("require", "Has worked in health or government.",
     'REQUIRE industries HAS ANY ("health", "government") BECAUSE "The client is a hospital network."'),
    ("prefer", "Prefers candidates with cloud certifications.",
     'PREFER certifications HAS ANY ("AWS Certified Solutions Architect") WEIGHT 1.5 BECAUSE "The team is on AWS."'),
    ("prefer", "Prefers depth in data tooling.",
     'PREFER MAX(skill.years WHERE name = "Spark") >= 2 WEIGHT 1.0 BECAUSE "The pipeline is Spark-based."'),
    ("prefer", "Prefers people who have shipped side projects.",
     'PREFER project_count >= 2 WEIGHT 0.8 BECAUSE "Self-directed delivery matters on a small team."'),
    ("prefer", "Prefers more total experience.",
     'PREFER years_experience >= 8 WEIGHT 1.2 BECAUSE "Depth reduces ramp-up time."'),
    ("prefer", "Prefers management experience.",
     'PREFER management_years >= 2 WEIGHT 0.9 BECAUSE "The role mentors juniors."'),
]

PLANS = [
    "We need someone who can own a backend service end to end, with real depth in Python and "
    "cloud infrastructure. Team leadership is a plus, not a requirement.",
    "Hiring for a data platform role. Must be able to work unrestricted in Australia. "
    "We care about pipeline experience far more than about where someone studied.",
    "Ward nursing role, AHPRA registration essential, health sector experience preferred.",
    "Graduate intake. We want potential, not years — a completed degree and evidence of "
    "self-directed projects.",
]


def _profile(rng: random.Random, ref: str) -> AnonymizedProfile:
    """One de-identified profile, in the shape the anonymization pass emits."""
    years = round(rng.triangular(0.5, 22, 6), 1)
    skills = [
        Skill(name=name, category="technical", years=round(min(years, rng.uniform(0.5, years + 1)), 1),
              proficiency=rng.choice(["intermediate", "advanced", "expert", None]),
              evidence="Named across multiple roles.")
        for name in rng.sample(TECH, rng.randint(3, 9))
    ]
    skills += [Skill(name=name, category="domain", years=None) for name in rng.sample(DOMAIN, rng.randint(0, 2))]
    skills += [Skill(name=name, category="tool", years=None) for name in rng.sample(TOOLS, rng.randint(0, 3))]

    roles = []
    remaining = years
    for _ in range(rng.randint(1, 4)):
        months = round(min(remaining, rng.uniform(0.6, 4.5)) * 12)
        if months <= 0:
            break
        remaining -= months / 12
        roles.append(Experience(
            title=rng.choice(TITLES), employer=rng.choice(EMPLOYERS),
            months=months, is_current=not roles,
            seniority=rng.choice(SENIORITY), industry=rng.choice(INDUSTRIES),
            employment_type=rng.choice(["permanent", "contract", "casual"]),
            team_size=rng.choice([None, None, 3, 5, 8, 12]),
            technologies=rng.sample(TECH, rng.randint(1, 4)),
            summary="Delivered and operated production systems for the team.",
        ))

    title, field, level, label = rng.choice(QUALS)
    quals = [Qualification(title=title, field_of_study=field, aqf_level=level, aqf_label=label,
                           aqf_confidence=0.95, completion_year=rng.randint(2005, 2025))]
    # Not everyone has a degree; the rule engine must handle that as unknown-free zero.
    if rng.random() < 0.15:
        quals = []

    status = rng.choices(
        [WorkRightsStatus.CITIZEN, WorkRightsStatus.PERMANENT_RESIDENT, WorkRightsStatus.VISA_UNRESTRICTED,
         WorkRightsStatus.VISA_RESTRICTED, WorkRightsStatus.UNKNOWN],
        weights=[45, 20, 10, 10, 15])[0]
    unrestricted = None if status is WorkRightsStatus.UNKNOWN else status is not WorkRightsStatus.VISA_RESTRICTED

    return AnonymizedProfile(
        candidate_ref=ref,
        summary=f"{rng.choice(TITLES)} with {years:.0f} years across {rng.choice(INDUSTRIES)}.",
        skills=skills, experience=roles, total_years_experience=years, qualifications=quals,
        institution_tiers=[rng.choice(TIERS)] if quals else [],
        region=rng.choice(REGIONS),
        work_rights=WorkRights(status=status, unrestricted=unrestricted,
                               evidence="Stated on the resume." if status is not WorkRightsStatus.UNKNOWN else None),
        languages=["English"] + (["Mandarin"] if rng.random() < 0.2 else []),
        certifications=rng.sample(CERTS, rng.randint(0, 3)),
        licences=rng.sample(LICENCES, rng.randint(0, 2)),
        management_years=rng.choice([None, 0.0, 1.5, 3.0, 6.0]),
        people_managed_max=rng.choice([None, None, 2, 4, 7, 15]),
        publications_count=rng.choice([None, None, 0, 1, 4]),
        availability_weeks=rng.choice([None, 2.0, 4.0, 6.0, 12.0]),
    )


def _rule_set(rng: random.Random) -> RuleSet:
    """A compiled rule set: real clauses, parsed by the real parser."""
    picks = rng.sample([r for r in RULE_BANK if r[0] == "require"], rng.randint(1, 3))
    picks += rng.sample([r for r in RULE_BANK if r[0] == "prefer"], rng.randint(1, 3))
    rules = []
    for index, (kind, text, dsl) in enumerate(picks, start=1):
        rules.append(ClassifiedRule(
            id=f"rule_{index}", source_text=text, verdict=RuleVerdict.APPLICABLE,
            dsl=dsl, clause=parse_clause(dsl), kind=kind,
            justification="Tied to a duty of the role, not to any protected attribute.",
        ))
    return RuleSet(rules=rules, source_plan=rng.choice(PLANS),
                   reasoning="Each requirement was traced to a duty of the role; nothing in the "
                             "plan screened on a protected attribute.")


def _score(rng: random.Random, profile: AnonymizedProfile, rule_set: RuleSet) -> CandidateScore:
    """A deterministic stand-in for the ranking pass, read off the profile."""
    criteria = []
    years = profile.total_years_experience or 0.0
    criteria.append(CriterionScore(criterion="experience_depth", score=min(1.0, years / 12),
                                   weight=2.0, evidence=f"total_years_experience = {years}"))
    criteria.append(CriterionScore(criterion="required_skills", score=min(1.0, len(profile.skills) / 10),
                                   weight=3.0, evidence=f"{len(profile.skills)} skills stated"))
    criteria.append(CriterionScore(criterion="qualification",
                                   score=min(1.0, (profile.highest_aqf or 0) / 9), weight=1.0,
                                   evidence=f"highest_aqf = {profile.highest_aqf}"))
    criteria.append(CriterionScore(criterion="role_relevance", score=round(rng.uniform(0.3, 1.0), 2),
                                   weight=1.5, evidence="Prior titles resemble the role."))
    total = sum(c.score * c.weight for c in criteria) / sum(c.weight for c in criteria)
    return CandidateScore(
        candidate_ref=profile.candidate_ref, score=round(min(1.0, total), 3), criteria=criteria,
        rationale="Scored on stated experience, skills and qualification level.",
        model="synthetic-seed", pass_name="triage",
    )


def seed(db_path: str | None, flagship: int, batches: int, runs_total: int, seed_value: int) -> None:
    rng = random.Random(seed_value)
    store = Store(db_path)
    conn = sqlite3.connect(str(store.db_path))  # bulk writes; the store owns the schema
    conn.execute("PRAGMA journal_mode=WAL")
    started = time.time()
    now = datetime.now(timezone.utc)

    def stamp(days_ago: float) -> str:
        return (now - timedelta(days=days_ago)).isoformat()

    plans: list[tuple[str, str, int, float]] = [("demo-corpus", "Demo corpus (synthetic)", flagship, 30.0)]
    for index in range(batches - 1):
        plans.append((
            f"intake-{index + 1:03d}",
            f"{rng.choice(['Weekly intake', 'Careers page', 'Seek campaign', 'Referral pool', 'Graduate drive'])}"
            f" {index + 1:03d}",
            rng.randint(18, 90),
            rng.uniform(1, 120),
        ))

    profiles: dict[str, list[tuple[str, AnonymizedProfile]]] = {}
    candidate_rows, batch_rows, audit_rows = [], [], []
    ref_counter = 0
    for batch_id, name, size, age in plans:
        created = stamp(age)
        batch_rows.append((batch_id, name, BatchStatus.COMPLETE.value,
                           json.dumps({"kind": "synthetic", "synthetic": True, "documents": size}),
                           None, created, created))
        bucket = []
        for index in range(1, size + 1):
            ref_counter += 1
            candidate_id = f"cand_seed_{uuid.uuid4().hex[:12]}"
            ref = f"Candidate {index}"
            profile = _profile(rng, ref)
            bucket.append((candidate_id, profile))
            candidate_rows.append((
                candidate_id, batch_id, f"resume_{index:05d}.pdf", f"seed:{candidate_id}",
                CandidateStatus.READY.value, ref, None, None, None,
                profile.model_dump_json(), None, created, created,
            ))
        profiles[batch_id] = bucket
        audit_rows.append((batch_id, None, None, created, "ingestion", "batch_created",
                           json.dumps({"accepted": size, "duplicates": 0, "name": name, "synthetic": True})))
        audit_rows.append((batch_id, None, None, created, "ingestion", "batch_complete",
                           json.dumps({"ready": size, "needs_manual_review": 0, "failed": 0,
                                       "duplicate": 0, "synthetic": True})))

    conn.executemany("INSERT OR REPLACE INTO batches (id, name, status, source_json, error, created_at,"
                     " updated_at) VALUES (?,?,?,?,?,?,?)", batch_rows)
    conn.executemany("INSERT OR REPLACE INTO candidates (id, batch_id, filename, content_hash, status,"
                     " candidate_ref, duplicate_of, extraction_json, structured_json, anonymized_json,"
                     " error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", candidate_rows)
    conn.commit()
    print(f"{len(batch_rows)} batches, {len(candidate_rows)} candidates in {time.time() - started:.1f}s",
          flush=True)

    # Runs. The flagship batch carries a handful over its whole corpus; the
    # rest are spread over the smaller intakes, which is what a year of real
    # use looks like.
    run_rows, result_rows = [], []
    weights = [12 if batch_id == "demo-corpus" else 1 for batch_id, *_ in plans]
    for number in range(1, runs_total + 1):
        batch_id, batch_name, size, age = rng.choices(plans, weights=weights)[0]
        created = stamp(max(0.1, age * rng.uniform(0.05, 0.95)))
        run_id = f"run_seed_{uuid.uuid4().hex[:12]}"
        role = RoleSpec(title=rng.choice(ROLE_TITLES),
                        description="Seeded analysis run over a synthetic corpus.")
        rule_set = _rule_set(rng)
        run_name = f"{role.title} — {batch_name} #{number}"

        screening = {}
        scores = []
        for candidate_id, profile in profiles[batch_id]:
            result = screen(profile, rule_set, None)
            screening[profile.candidate_ref] = result
            if not result.eligible:
                outcome = CandidateOutcome.EXCLUDED
            elif result.needs_manual_review:
                outcome = CandidateOutcome.NEEDS_MANUAL_REVIEW
            else:
                outcome = CandidateOutcome.ELIGIBLE
            score = _score(rng, profile, rule_set) if result.eligible else None
            if score is not None:
                scores.append(score)
            result_rows.append((run_id, candidate_id, profile.candidate_ref, outcome.value,
                                json.dumps(result.to_dict(), default=str),
                                score.model_dump_json() if score else None, created))
        shortlist = build_shortlist(scores, role, screening=screening)
        run_rows.append((run_id, batch_id, run_name, RunStatus.COMPLETE.value,
                         role.model_dump_json(), rule_set.model_dump_json(),
                         shortlist.model_dump_json(), None, created, created))
        audit_rows.append((batch_id, run_id, None, created, "runs", "run_created",
                           json.dumps({"run_id": run_id, "name": run_name, "role": role.title,
                                       "candidates": size, "synthetic": True})))
        audit_rows.append((batch_id, run_id, None, created, "rules", "plan_compiled",
                           json.dumps({"plan": rule_set.source_plan, "reasoning": rule_set.reasoning,
                                       "rules": len(rule_set.rules), "applied": len(rule_set.applied),
                                       "flagged": 0, "synthetic": True})))
        audit_rows.append((batch_id, run_id, None, created, "ranking", "triage_started",
                           json.dumps({"eligible": len(scores), "screened": size, "synthetic": True})))
        audit_rows.append((batch_id, run_id, None, created, "runs", "run_complete",
                           json.dumps({"shortlisted": len(shortlist.entries),
                                       "excluded": len(shortlist.excluded),
                                       "manual_review": len(shortlist.manual_review),
                                       "synthetic": True})))
        if number % 250 == 0:
            conn.executemany("INSERT OR REPLACE INTO runs (id, batch_id, name, status, role_json,"
                             " rules_json, shortlist_json, error, created_at, updated_at)"
                             " VALUES (?,?,?,?,?,?,?,?,?,?)", run_rows)
            conn.executemany("INSERT OR REPLACE INTO run_results (run_id, candidate_id, candidate_ref,"
                             " outcome, screening_json, score_json, updated_at) VALUES (?,?,?,?,?,?,?)",
                             result_rows)
            conn.commit()
            print(f"  {number}/{runs_total} runs, {len(result_rows)} results "
                  f"({time.time() - started:.0f}s)", flush=True)
            run_rows, result_rows = [], []

    conn.executemany("INSERT OR REPLACE INTO runs (id, batch_id, name, status, role_json, rules_json,"
                     " shortlist_json, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", run_rows)
    conn.executemany("INSERT OR REPLACE INTO run_results (run_id, candidate_id, candidate_ref, outcome,"
                     " screening_json, score_json, updated_at) VALUES (?,?,?,?,?,?,?)", result_rows)
    conn.executemany("INSERT INTO audit (batch_id, run_id, candidate_id, at, stage, event, detail_json)"
                     " VALUES (?,?,?,?,?,?,?)", audit_rows)
    conn.commit()

    counts = dict(conn.execute("SELECT 'batches', COUNT(*) FROM batches UNION ALL"
                               " SELECT 'candidates', COUNT(*) FROM candidates UNION ALL"
                               " SELECT 'runs', COUNT(*) FROM runs UNION ALL"
                               " SELECT 'results', COUNT(*) FROM run_results UNION ALL"
                               " SELECT 'audit', COUNT(*) FROM audit").fetchall())
    conn.close()
    print(f"seeded in {time.time() - started:.0f}s: {counts}", flush=True)


def reset_synthetic(db_path: str | None) -> None:
    """Remove seeded rows, leaving anything the real pipeline produced."""
    store = Store(db_path)
    conn = sqlite3.connect(str(store.db_path))
    conn.execute("DELETE FROM run_results WHERE run_id LIKE 'run_seed_%'")
    conn.execute("DELETE FROM audit WHERE run_id LIKE 'run_seed_%' OR candidate_id LIKE 'cand_seed_%'"
                 " OR batch_id IN (SELECT id FROM batches WHERE source_json LIKE '%\"synthetic\": true%')")
    conn.execute("DELETE FROM runs WHERE id LIKE 'run_seed_%'")
    conn.execute("DELETE FROM candidates WHERE id LIKE 'cand_seed_%'")
    conn.execute("DELETE FROM batches WHERE source_json LIKE '%\"synthetic\": true%'")
    conn.commit()
    conn.close()
    print("synthetic rows removed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="database path (default: RESCAN_DB_PATH)")
    parser.add_argument("--flagship", type=int, default=1200, help="candidates in the demo batch")
    parser.add_argument("--batches", type=int, default=40)
    parser.add_argument("--runs", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--reset-synthetic", action="store_true", help="delete seeded rows and exit")
    args = parser.parse_args()
    if args.reset_synthetic:
        reset_synthetic(args.db)
        return
    seed(args.db, args.flagship, args.batches, args.runs, args.seed)


if __name__ == "__main__":
    main()
