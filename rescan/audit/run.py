"""CLI for the bias audit.

    python -m rescan.audit.run --source synthetic --out data/bias_audit.json

Run it against the served model, not the stub, for a result that means
anything:

    RESCAN_LLM_BACKEND=openai RESCAN_LLM_BASE_URL=http://gpu:8000/v1 \
        python -m rescan.audit.run --source synthetic
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rescan.audit.bias import run_bias_audit
from rescan.audit.corpus import huggingface_corpus, synthetic_corpus, variant_count
from rescan.config import REPO_ROOT, settings
from rescan.llm.client import build_client
from rescan.schemas import RoleSpec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the score gap with and without anonymization.")
    parser.add_argument("--source", choices=("synthetic", "huggingface"), default="synthetic")
    parser.add_argument("--limit", type=int, default=20, help="Cases to pull from the study dataset.")
    parser.add_argument("--names-per-group", type=int, default=1)
    parser.add_argument("--proxies", action="store_true", help="Also vary institution and suburb.")
    parser.add_argument("--role", default="Senior Backend Engineer")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.source == "synthetic":
        cases = synthetic_corpus(
            REPO_ROOT / "data" / "samples",
            names_per_group=args.names_per_group,
            include_proxies=args.proxies,
        )
    else:
        cases = huggingface_corpus(
            limit=args.limit, cache_path=settings.data_dir / "bias_corpus.json"
        )
        if not cases:
            print(
                "No cases retrieved from the study dataset. It may still be "
                "indexing; rerun, or use --source synthetic.",
                file=sys.stderr,
            )
            return 1

    print(f"corpus: {len(cases)} cases, {variant_count(cases)} variants", file=sys.stderr)

    role = RoleSpec(
        title=args.role,
        required_skills=["Python"],
        desirable_skills=["AWS", "Kubernetes"],
        min_years_experience=5,
        min_aqf=7,
    )
    report = run_bias_audit(cases, build_client(), role, workers=args.workers)
    print(report.summary_table())

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
        print(f"\nwritten to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
