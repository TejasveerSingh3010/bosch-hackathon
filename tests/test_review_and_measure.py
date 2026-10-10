"""The review round, fix recipes, reasoning effort, scores and the regression replay."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import remediation.recommend as recommend
from agents import tracing
from remediation.agent import GroqProposer, critique_context, fix_recipes
from remediation.evaluate import evaluate, summarise
from remediation.models import CodeProposal, Critique
from remediation.recommend import review_and_revise
from remediation.store import Store
from reporter.demo import run_demo
from tests.test_code_remediation import DOCS, checkout, make_code_draft  # noqa: F401 - fixture
from tests.test_remediation import make_draft, make_proposal, make_result, write_record

DECLINED = make_proposal(
    make_draft(remediable=False, changes=[], declined_reason="cart errors are fatal"), pid="rem-declined"
)


class Reviewer:
    """A proposer whose review says `issues`, and whose rewrite returns `rewrite`."""

    model_name = "fake"

    def __init__(self, issues: list[str], fixes: bool = True, rewrite=None) -> None:
        self.issues, self.fixes, self.rewrite = issues, fixes, rewrite
        self.critiques: list[str] = []

    def critique_code(self, context: str) -> Critique:
        self.critiques.append(context)
        return Critique(fixes_diagnosis=self.fixes, issues=self.issues)

    def propose_code(self, context: str):
        return self.rewrite


@pytest.fixture
def clean_record(results_dir):
    write_record(results_dir, make_result("kill-cart-1"))  # what make_code_draft's evidence cites


@pytest.fixture
def contexts(monkeypatch):
    """Revision contexts, without building a full code context from the real services.yaml."""
    seen: list[str] = []
    def build(*_args, extra="", **_kwargs):
        seen.append(extra)
        return extra

    monkeypatch.setattr(recommend, "build_code_context", build)
    return seen


def test_a_sound_draft_is_reviewed_once_and_kept(config, checkout, clean_record, contexts):  # noqa: F811
    draft = make_code_draft()
    reviewer = Reviewer(issues=[])
    final, issues, revised = review_and_revise(reviewer, draft, DECLINED, [], config, checkout, DOCS, None)
    assert final is draft and issues == [] and not revised and contexts == []
    assert "```diff" in reviewer.critiques[0] and "homeHandler" in reviewer.critiques[0]


def test_review_issues_earn_exactly_one_rewrite(config, checkout, clean_record, contexts):  # noqa: F811
    draft, better = make_code_draft(), make_code_draft(expected_benefit="better")
    issue = "frontend/handlers.go (homeHandler): also guard productHandler"
    reviewer = Reviewer(issues=[issue], rewrite=better)
    final, issues, revised = review_and_revise(reviewer, draft, DECLINED, [], config, checkout, DOCS, None)
    assert final is better and revised and issues == [f"review: {reviewer.issues[0]}"]
    (extra,) = contexts
    assert "Your previous draft" in extra and "also guard productHandler" in extra


def test_automated_findings_reach_the_reviewer_and_the_rewrite(config, checkout, contexts):  # noqa: F811
    # No record behind the evidence: the policy flags it, and that alone earns a rewrite.
    reviewer = Reviewer(issues=[], rewrite=make_code_draft())
    _, issues, revised = review_and_revise(
        reviewer, make_code_draft(), DECLINED, [], config, checkout, DOCS, None
    )
    assert revised and issues and "Automated checks found" in reviewer.critiques[0]


def test_a_reviewer_that_says_it_misses_the_cause_is_heard(config, checkout, clean_record, contexts):  # noqa: F811
    reviewer = Reviewer(issues=[], fixes=False, rewrite=make_code_draft())
    draft = make_code_draft()
    _, issues, revised = review_and_revise(reviewer, draft, DECLINED, [], config, checkout, DOCS, None)
    assert revised and issues == ["review: the patch does not remove the diagnosed cause"]


def test_synthetic_data_is_not_worth_a_review(config, checkout, results_dir, tmp_path, contexts):  # noqa: F811
    reports = tmp_path / "reports"
    run_demo("kill", results_dir, reports)
    evidence = make_code_draft().evidence[0].model_copy(
        update={"result_id": "demo-kill-redis-cart", "field": "experiment.action", "observed": "kill"}
    )
    reviewer = Reviewer(issues=["x"])
    draft = make_code_draft(evidence=[evidence])
    final, issues, revised = review_and_revise(reviewer, draft, DECLINED, [], config, checkout, DOCS, reports)
    assert final is draft and not revised and reviewer.critiques == []


def test_the_critique_context_is_small():
    context = critique_context(make_code_draft(), DECLINED, "--- a\n+++ b\n", ["x is wrong"])
    assert len(context) < 3000 and "x is wrong" in context


# ---------------------------------------------------------------- recipes, effort
def test_recipes_apply_by_fault():
    raw = {"fix_recipes": {"deadline": {"faults": ["pause"], "recipe": "add one"}, "any": {"recipe": "x"}}}
    pause, kill = make_result("p"), make_result("k")
    pause.experiment.action = "pause"
    assert "deadline" in fix_recipes(raw, [pause]) and "deadline" not in fix_recipes(raw, [kill])
    assert "any: x" in fix_recipes(raw, [kill])


def test_the_real_recipes_cite_upstream_code():
    import yaml

    from remediation.policy import SERVICE_DOCS

    recipes = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8"))["fix_recipes"]
    assert {"call_deadline", "optional_dependency", "datastore_reads_only"} <= set(recipes)
    cites = (".go:", ".cs", "Async")
    assert all(any(c in r["recipe"] for c in cites) for r in recipes.values())


def test_code_asks_for_more_reasoning_and_falls_back_if_cut_off(monkeypatch):
    import remediation.agent as agent

    monkeypatch.setattr(agent, "CODE_REASONING_EFFORT", "high")
    efforts = []

    def create(**kwargs):
        efforts.append(kwargs["reasoning_effort"])
        cut = len(efforts) == 1
        content = "" if cut else make_code_draft().model_dump_json()
        msg = SimpleNamespace(content=content)
        choice = SimpleNamespace(finish_reason="length" if cut else "stop", message=msg)
        return SimpleNamespace(choices=[choice])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert GroqProposer(client=client).propose_code("ctx") == make_code_draft()
    assert efforts == ["high", "medium"]


def test_a_schema_break_counts_as_a_cut_off():
    import groq

    from remediation.agent import CutOff

    def create(**_kwargs):
        response = SimpleNamespace(status_code=400, headers={}, request=None)
        message = "Failed to generate JSON (json_validate_failed)"
        raise groq.BadRequestError(message, response=response, body=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(CutOff):
        GroqProposer(client=client).propose("ctx")


# ---------------------------------------------------------------- scores, replay
def test_the_human_decision_is_scored_on_the_proposals_trace(results_dir, monkeypatch):
    sent = []
    monkeypatch.setattr(tracing, "score", lambda trace_id, name, value, comment=None, **_: sent.append(
        (trace_id, name, value, comment)
    ))
    proposal = make_proposal()
    proposal.trace_id = "abc123"
    store = Store(results_dir)
    store.save_proposal(proposal, {"cartservice": {}})
    store.record_decision(proposal.id, "rejected", "tester", "misses the cause")
    assert sent == [("abc123", "human_decision", 0, "misses the cause")]


def test_the_replay_finds_proposals_todays_checks_reject(results_dir, config, checkout, clean_record):  # noqa: F811
    store = Store(results_dir)
    silent = make_code_draft()
    # A new `if err != nil { return }` after the upstream branch: the error is dropped.
    silent.edits[0].original = (
        "\t\trenderHTTPError(log, r, w, err, http.StatusInternalServerError)\n\t\treturn\n"
    )
    silent.edits[0].replacement = "\t}\n\tif err != nil {\n\t\treturn\n"
    filed = CodeProposal(
        **silent.model_dump(), id="rem-old", author="llm:x", created_at=datetime.now().astimezone(),
        responds_to="rem-declined", source_commit="abc",
    )
    store.save_proposal(filed, {})
    store.record_decision("rem-old", "approved", "tester")
    (row,) = evaluate(config, {}, checkout, DOCS, None)
    assert row.clean_then and not row.clean_now
    summary = summarise([row])
    assert summary["newly_flagged"] == ["rem-old"] and summary["disagreements"] == ["rem-old"]


def test_the_wire_schema_moves_guidance_into_the_prompt():
    import json

    from remediation.agent import CODE_SYSTEM_PROMPT, wire_schema
    from remediation.models import CodeProposalDraft

    system, schema = wire_schema(CodeProposalDraft, CODE_SYSTEM_PROMPT)
    text = json.dumps(schema)
    assert '"description"' not in text and '"title"' not in text
    assert schema["$defs"]["CodeEdit"]["required"]  # still strict
    assert "- CodeEdit.original: Exact text to replace" in system
