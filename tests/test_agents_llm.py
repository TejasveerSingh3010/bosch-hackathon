"""The LLM seam: the stub is the default, the real client is a guarded placeholder, and the
validate -> retry-once -> fail-loudly -> audit-log behaviour holds whatever provider is plugged in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from agents.llm import (
    AgentLog,
    LLMOutputError,
    NotConfigured,
    RealClient,
    StubClient,
    get_client,
)


class Answer(BaseModel):
    harm: int
    why: str


def test_stub_returns_scripted_json_and_validates(results_dir: Path):
    stub = StubClient(scripted=['{"harm": 80, "why": "redis is a SPOF"}'], results_dir=results_dir)
    out = stub.generate(agent="planner", system="s", user="u", schema=Answer, evidence_ids=["E1"])
    assert out.harm == 80 and out.why == "redis is a SPOF"


def test_a_bad_response_is_retried_once_then_succeeds(results_dir: Path):
    stub = StubClient(scripted=["not json at all", '{"harm": 10, "why": "ok"}'], results_dir=results_dir)
    out = stub.generate(agent="planner", system="s", user="u", schema=Answer)
    assert out.harm == 10
    assert len(stub.calls) == 2  # one failed, one retry


def test_two_bad_responses_raise_loudly_and_save_raw(results_dir: Path):
    stub = StubClient(scripted=["garbage one", "garbage two"], results_dir=results_dir)
    with pytest.raises(LLMOutputError, match="failed validation twice"):
        stub.generate(agent="reporter", system="s", user="u", schema=Answer)
    assert (results_dir / "reporter.raw.txt").read_text(encoding="utf-8") == "garbage two"


def test_json_is_extracted_from_code_fences_and_prose(results_dir: Path):
    fenced = 'Here you go:\n```json\n{"harm": 5, "why": "fine"}\n```\nhope that helps'
    stub = StubClient(scripted=[fenced], results_dir=results_dir)
    out = stub.generate(agent="planner", system="s", user="u", schema=Answer)
    assert out.harm == 5


def test_every_call_is_logged_to_agent_log_jsonl(results_dir: Path):
    stub = StubClient(scripted=['{"harm": 1, "why": "x"}'], results_dir=results_dir)
    stub.generate(agent="advisor", system="s", user="u", schema=Answer, evidence_ids=["E1", "E2"])
    lines = (results_dir / "agent_log.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["agent"] == "advisor"
    assert entry["model"] == "stub"
    assert entry["evidence_ids"] == ["E1", "E2"]
    assert entry["output"]["harm"] == 1
    assert "prompt_hash" in entry


def test_a_failed_call_is_also_logged_with_the_error(results_dir: Path):
    stub = StubClient(scripted=["bad", "bad"], results_dir=results_dir)
    with pytest.raises(LLMOutputError):
        stub.generate(agent="planner", system="s", user="u", schema=Answer)
    entry = json.loads((results_dir / "agent_log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["output"] is None
    assert entry["error"]


def test_real_client_is_a_placeholder_that_refuses_until_implemented(results_dir: Path):
    client = RealClient(provider="example", model="m", api_key="k", results_dir=results_dir)
    with pytest.raises(NotConfigured, match="not implemented yet"):
        client.generate(agent="planner", system="s", user="u", schema=Answer)


def test_get_client_is_none_without_a_key(results_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """The Phase 1 state: no key -> deterministic path. .env is read lazily, not at import."""
    monkeypatch.setattr("agents.llm._load_env_file", lambda *_a, **_k: {})
    for var in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    assert get_client(results_dir) is None


def test_get_client_builds_a_real_client_when_a_key_is_present(
    results_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("agents.llm._load_env_file", lambda *_a, **_k: {})
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_PROVIDER", "example")
    monkeypatch.setenv("LLM_MODEL", "m-1")
    client = get_client(results_dir)
    assert isinstance(client, RealClient)
    assert client.provider == "example" and client.model == "m-1"


def test_env_file_is_read_when_the_process_env_is_empty(
    results_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = tmp_path / ".env"
    env.write_text('LLM_API_KEY="from-file"\nLLM_PROVIDER=p\nLLM_MODEL=m\n', encoding="utf-8")
    monkeypatch.setattr("agents.llm.ENV_FILE", env)
    for var in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    client = get_client(results_dir)
    assert isinstance(client, RealClient) and client.api_key == "from-file"


def test_agent_log_never_raises_on_a_bad_directory(tmp_path: Path):
    # Pointing the log at a path under a file (not a dir) must be swallowed, never crash an agent.
    wall = tmp_path / "wall"
    wall.write_text("x", encoding="utf-8")
    log = AgentLog(wall / "nested")
    log.record(agent="a", model="m", prompt_hash="h", evidence_ids=[], output={"ok": True})
    assert not os.path.exists(wall / "nested")
