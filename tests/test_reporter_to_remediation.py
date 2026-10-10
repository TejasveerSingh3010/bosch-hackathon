"""Agent 2 (reporter) -> Agent 3 (remediation): the report reaches the remediation context, and the
code-recommendation request fits Groq's per-minute token limit."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from remediation.agent import (
    CODE_SYSTEM_PROMPT,
    GROQ_TPM,
    MAX_REQUEST_TOKENS,
    GroqProposer,
    ProposalError,
    build_code_context,
    build_context,
    code_excerpts,
    estimate_request,
    experiment_records,
    report_digest,
    wire_schema,
)
from remediation.models import CodeProposalDraft
from remediation.policy import SERVICE_DOCS
from remediation.source import CHECKOUT_DIR
from reporter.demo import run_demo
from tests.test_code_remediation import DOCS, checkout  # noqa: F401 - fixture
from tests.test_remediation import make_draft, make_proposal, make_result

PAUSE_ID = "demo-pause-productcatalogservice"


@pytest.fixture
def reported(results_dir: Path, tmp_path: Path) -> Path:
    """Agent 2's real output for the pause scenario: the record in results/, the report in reports/."""
    reports = tmp_path / "reports"
    run_demo("pause", results_dir, reports)
    return reports


def test_the_report_is_digested_for_the_remediation_agent(reported):
    digest = report_digest(PAUSE_ID, reported)
    assert "origin: productcatalogservice (observed)" in digest
    assert "frontend deadline_exceeded x14" in digest  # the log finding the records alone lack
    assert "spread: Home page +" in digest
    assert "synthetic" in digest  # the caveat travels with it
    assert report_digest("no-such-experiment", reported) is None


def test_the_config_context_carries_the_report_under_its_record(reported, config):
    records = experiment_records(config.results_dir, [PAUSE_ID])
    context = build_context(records, config, {}, reports_dir=reported)
    section = context.split(f"## {PAUSE_ID}", 1)[1]
    assert "reporter (Agent 2" in section and "deadline_exceeded" in section
    assert "reporter (Agent 2" not in build_context(records, config, {}, reports_dir=reported / "none")


def test_excerpts_stop_at_the_character_budget(checkout):  # noqa: F811
    record = make_result("kill-redis-1")
    record.experiment.target = record.service = "redis-cart"
    full, _ = code_excerpts([record], DOCS, checkout)
    tight, omitted = code_excerpts([record], DOCS, checkout, max_chars=len(full) // 2)
    assert len(tight) < len(full) and omitted
    # A file whose functions were all left out is not shown at all (no heading, no imports).
    for file in ("rpc.go", "handlers.go"):
        if any(o.startswith(f"frontend/{file}:") for o in omitted) and f"({file}" not in tight:
            assert f"### frontend: {file}" not in tight
    nothing, omitted = code_excerpts([record], DOCS, checkout, max_chars=0)
    assert nothing == "" and len(omitted) == 2


@pytest.mark.skipif(not (CHECKOUT_DIR / "src").exists(), reason="pinned upstream checkout not cloned")
def test_the_pause_code_request_fits_groqs_minute_limit(reported, config):
    """The live failure: answering a productcatalogservice pause asked Groq for 8,082 tokens."""
    records = experiment_records(config.results_dir, [PAUSE_ID])
    declined = make_proposal(
        make_draft(remediable=False, changes=[], declined_reason="callers have no per-call deadlines"),
        pid="rem-declined",
    )
    declined.source_results = [PAUSE_ID]
    context = build_code_context(declined, records, CHECKOUT_DIR, SERVICE_DOCS, reports_dir=reported)
    system, schema = wire_schema(CodeProposalDraft, CODE_SYSTEM_PROMPT)
    assert estimate_request(system, context, json.dumps(schema)) <= MAX_REQUEST_TOKENS
    assert "reporter (Agent 2" in context
    # The fault's own call sites still make it in: the caller code a deadline fix would edit.
    docs = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8"))["services"]
    sites = docs["frontend"]["code"]["calls"]["productcatalogservice"]["functions"]
    assert any(f"({name})" in context for name in sites)


def test_an_oversized_request_is_refused_before_it_is_sent():
    sent = []
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: sent.append(kw)))
    )
    with pytest.raises(ProposalError, match="tokens a minute"):
        GroqProposer(client=client).propose("x" * int(GROQ_TPM * 4))
    assert sent == []
