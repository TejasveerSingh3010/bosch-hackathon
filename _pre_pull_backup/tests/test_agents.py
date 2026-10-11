"""The agent graph: its shape, every route ending in human escalation, and what Langfuse receives."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

import agents.remediation as remediation_agent
from agents import tracing
from agents.deps import Deps
from agents.graph import build_graph, run_pipeline
from agents.tracing import cost
from remediation.agent import GroqProposer
from remediation.models import CodeProposal
from remediation.recommend import Filed, new_id
from remediation.store import Store
from tests.test_code_remediation import make_code_draft
from tests.test_remediation import make_draft
from tests.test_runner import FakeClock, TelemetryScript, build_runner

PLAN = {"id": "exp-agents", "target": "adservice", "action": "pause", "duration_s": 20}


def groq_client(draft, usage=(1200, 300)):
    """A Groq SDK stand-in returning `draft`, with token usage like the real API reports it."""

    def create(**_kwargs):
        message = SimpleNamespace(content=draft.model_dump_json())
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=message)],
            usage=SimpleNamespace(
                prompt_tokens=usage[0],
                completion_tokens=usage[1],
                total_tokens=sum(usage),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=120),
            ),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def script(results_dir):
    return TelemetryScript(results_dir)


@pytest.fixture
def deps(docker, config, script):
    def make(draft=None, **kwargs):
        clock = FakeClock()
        return Deps(
            config=config,
            docker=docker,
            make_proposer=lambda: GroqProposer(client=groq_client(draft or make_draft())),
            build=None,
            make_runner=lambda d, c: build_runner(d, c, script, clock),
            **kwargs,
        )

    script.set()
    return make


# ---------------------------------------------------------------- shape
def test_the_graph_starts_at_the_attacker_and_ends_at_escalation(deps):
    graph = build_graph(deps()).get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert {t for s, t in edges if s == "__start__"} == {"attacker"}
    assert {s for s, t in edges if t == "__end__"} == {"escalate"}
    for agent in ("attacker", "analyst", "remediation"):
        # Own tools: agent <-> agent_tools, and out only through the handoff.
        assert {(agent, f"{agent}_tools"), (f"{agent}_tools", agent), (agent, f"{agent}_handoff")} <= edges
        assert {t for s, t in edges if s == agent} == {f"{agent}_tools", f"{agent}_handoff"}
    assert {t for s, t in edges if s == "attacker_handoff"} == {"analyst", "escalate"}
    assert {t for s, t in edges if s == "analyst_handoff"} == {"remediation", "escalate"}
    assert {t for s, t in edges if s == "remediation_handoff"} == {"escalate"}


# ---------------------------------------------------------------- routes
def test_a_full_run_files_a_recommendation_and_escalates_it(deps, results_dir):
    final, url = run_pipeline(deps(), {"plan": PLAN, "notes": []})
    assert url is None  # tracing is off in tests
    assert final["result_ids"] == ["exp-agents"] and final["findings"] == ["exp-agents"]
    report = final["escalation"]
    (item,) = report["recommendations"]
    assert item["kind"] == "config" and item["proposal_id"] == final["proposal_ids"][0]
    assert (results_dir / "remediation" / item["proposal_id"] / "proposal.json").exists()
    # Nothing is approved by the pipeline: the human decision is still to come.
    assert "approved" not in item["status"]
    names = [m.name for m in final["attacker_messages"] if m.type == "tool"]
    assert names == ["run_experiment"]


def test_a_rejected_plan_skips_to_escalation(deps):
    final, _ = run_pipeline(deps(), {"plan": {**PLAN, "target": "loadgenerator"}, "notes": []})
    assert not final.get("analyst_messages") and not final.get("remediation_messages")
    assert final["escalation"]["headline"] == "no recommendation was filed"
    assert any("attacker:" in n for n in final["escalation"]["notes"])


def test_a_dry_run_injects_nothing_and_still_escalates(deps, docker):
    final, _ = run_pipeline(deps(), {"plan": PLAN, "dry_run": True, "notes": []})
    assert not final.get("result_ids")
    assert not any(c.paused for c in docker.containers.values())
    assert "attacker: dry run, nothing was injected" in final["escalation"]["notes"]


def test_a_declined_config_fix_is_answered_with_code(deps, monkeypatch):
    asked = []

    def fake_code(_proposer, config, declined_id, _build):
        asked.append(declined_id)
        draft = make_code_draft(edits=[])
        proposal = CodeProposal(
            **draft.model_dump(), id=new_id(Store(config.results_dir)), author="human",
            created_at=datetime.now().astimezone(), responds_to=declined_id, source_commit="abc",
        )
        Store(config.results_dir).save_proposal(proposal, {})
        return Filed(proposals=[proposal])

    monkeypatch.setattr(remediation_agent, "recommend_code", fake_code)
    declined = make_draft(remediable=False, changes=[], declined_reason="needs a code change")
    final, _ = run_pipeline(deps(declined), {"plan": PLAN, "notes": []})
    tools = [m.name for m in final["remediation_messages"] if m.type == "tool"]
    assert tools == ["recommend_config_fix", "recommend_code_fix"]
    assert asked == [final["proposal_ids"][0]]
    assert [i["kind"] for i in final["escalation"]["recommendations"]] == ["config", "code"]

    inputs = {"plan": {**PLAN, "id": "exp-2"}, "allow_code": False, "notes": []}
    final, _ = run_pipeline(deps(declined), inputs)
    tools = [m.name for m in final["remediation_messages"] if m.type == "tool"]
    assert tools == ["recommend_config_fix"]
    assert final["escalation"]["headline"].startswith("nothing to approve")


def test_an_llm_error_is_a_note_not_a_crash(deps):
    def broken():
        from remediation.agent import ProposalError

        raise ProposalError("rate limited by the Groq API - try again shortly")

    d = deps()
    d.make_proposer = broken
    final, _ = run_pipeline(d, {"plan": PLAN, "notes": []})
    assert final["escalation"]["headline"] == "no recommendation was filed"
    assert any("rate limited" in n for n in final["escalation"]["notes"])


# ---------------------------------------------------------------- tracing
def test_cost_uses_the_price_table():
    assert cost("openai/gpt-oss-120b", {"input": 1_000_000, "output": 1_000_000}) == {
        "input": 0.15, "output": 0.60, "total": 0.75,
    }
    assert cost("some/unpriced-model", {"input": 10}) is None


def test_langfuse_gets_one_trace_with_tools_tokens_and_cost(deps, monkeypatch):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    scores: list[tuple] = []  # scores go over the API, not OTel: capture them instead of a dead socket

    def record(trace_id, name, _value, *_args, **_kwargs):
        scores.append((trace_id, name))

    monkeypatch.setattr(tracing, "score", record)
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        base_url="http://127.0.0.1:9",
        span_exporter=exporter,
    )
    try:
        run_pipeline(deps(), {"plan": PLAN, "notes": []}, session_id="sess-1")
        client.flush()
    finally:
        client.shutdown()
    spans = exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}

    def ancestors(span):
        while span.parent is not None and span.parent.span_id in by_id:
            span = by_id[span.parent.span_id]
            yield span.name

    assert len({s.context.trace_id for s in spans}) == 1
    names = {s.name for s in spans}
    assert {"chaos-pipeline", "attacker", "analyst", "remediation", "escalate"} <= names
    assert {"run_experiment", "read_experiment_record", "recommend_config_fix"} <= names
    assert all(s.attributes.get("session.id") == "sess-1" for s in spans)

    tool = next(s for s in spans if s.name == "run_experiment")
    assert "exp-agents" in str(tool.attributes.get("langfuse.observation.output"))

    (llm,) = [s for s in spans if s.name == "remediation_proposal"]
    assert "recommend_config_fix" in set(ancestors(llm))
    assert json.loads(llm.attributes["langfuse.observation.usage_details"]) == {
        "input": 1200, "output": 300, "total": 1500,
    }
    spent = json.loads(llm.attributes["langfuse.observation.cost_details"])
    assert spent["total"] == pytest.approx((1200 * 0.15 + 300 * 0.60) / 1e6)
    assert llm.attributes["langfuse.observation.model.name"] == "openai/gpt-oss-120b"

    # The filed proposal carries its trace, and the filing verdict is scored on that same trace.
    trace_id = format(llm.context.trace_id, "032x")
    assert ("policy_clean" in {name for _, name in scores}) and {t for t, _ in scores} == {trace_id}
    (pid,) = Store(deps().config.results_dir).ids()
    assert Store(deps().config.results_dir).load_proposal(pid).trace_id == trace_id
