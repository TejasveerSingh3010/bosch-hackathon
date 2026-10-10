"""The command line and the experiment schema are the two interfaces humans and the day-2 AI use."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.__main__ import _experiment_from_args, build_parser, main
from schemas import Experiment, ExperimentResult

DOCS = Path(__file__).resolve().parent.parent / "docs"


# ------------------------------------------------------------------- schema
def test_the_documented_example_experiment_is_valid():
    """docs/experiment.example.json is the contract the planner will generate against."""
    payload = json.loads((DOCS / "experiment.example.json").read_text(encoding="utf-8"))
    experiment = Experiment.model_validate(payload)
    assert experiment.target == "redis-cart"
    assert experiment.abort_if.error_rate == 0.2  # a fraction, as documented


def test_an_unknown_action_is_rejected_by_the_schema():
    with pytest.raises(ValueError, match="kill"):
        Experiment(id="x", target="redis-cart", action="delete_everything", duration_s=10)


def test_an_unexpected_field_is_rejected_rather_than_ignored():
    """A hallucinated field should be a loud error, not silently dropped."""
    with pytest.raises(ValueError):
        Experiment(id="x", target="redis-cart", action="kill", duration_s=10, sudo=True)


def test_an_id_with_path_characters_is_rejected():
    """The id becomes a filename, so it must not be able to escape the results directory."""
    for bad in ("../escape", "a/b", "c:\\windows", "with space"):
        with pytest.raises(ValueError):
            Experiment(id=bad, target="redis-cart", action="kill", duration_s=10)


def test_a_result_round_trips_through_json():
    result = ExperimentResult(
        experiment=Experiment(id="r1", target="redis-cart", action="kill", duration_s=10),
        verdict="completed",
    )
    assert ExperimentResult.model_validate_json(result.model_dump_json()).verdict == "completed"


# ---------------------------------------------------------------------- CLI
def test_actions_subcommand_lists_every_failure_mode(capsys):
    assert main(["actions"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ("kill", "stop", "pause", "spike", "throttle"))


def test_flags_build_an_experiment_with_a_generated_id():
    args = build_parser().parse_args(["run", "--target", "adservice", "--action", "kill", "--duration", "20"])
    experiment = _experiment_from_args(args)
    assert (experiment.target, experiment.action, experiment.duration_s) == ("adservice", "kill", 20)
    assert experiment.id.startswith("kill-adservice-")


def test_a_plan_file_can_be_overridden_by_flags(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({
        "id": "fromfile", "target": "redis-cart", "action": "kill", "duration_s": 60,
    }), encoding="utf-8")
    args = build_parser().parse_args(["run", str(path), "--action", "pause", "--duration", "15"])
    experiment = _experiment_from_args(args)
    assert (experiment.id, experiment.action, experiment.duration_s) == ("fromfile", "pause", 15)


def test_spike_flags_become_rps_users_and_time():
    args = build_parser().parse_args(
        ["run", "--target", "frontend", "--action", "spike", "--duration", "20", "--rps", "15", "--users", "4"]
    )
    experiment = _experiment_from_args(args)
    assert experiment.duration_s == 20
    assert experiment.params == {"rps": 15, "users": 4, "time": 20}


def test_spike_without_a_rate_or_users_is_refused():
    args = build_parser().parse_args(["run", "--target", "frontend", "--action", "spike", "--duration", "20"])
    with pytest.raises(SystemExit, match="--rps"):
        _experiment_from_args(args)


def test_throttle_flag_becomes_the_cpu_limit_and_is_validated():
    args = build_parser().parse_args(
        ["run", "--target", "currencyservice", "--action", "throttle", "--cpus", "0.05"]
    )
    assert _experiment_from_args(args).params == {"cpus": 0.05}
    with pytest.raises(ValueError, match="cpus"):
        Experiment(id="t1", target="currencyservice", action="throttle", duration_s=10)
    args = build_parser().parse_args(["run", "--target", "adservice", "--action", "kill", "--cpus", "0.1"])
    with pytest.raises(SystemExit, match="throttle"):
        _experiment_from_args(args)


def test_spike_time_cannot_outlast_the_approved_duration():
    with pytest.raises(ValueError, match="duration_s"):
        Experiment(
            id="s1", target="frontend", action="spike", duration_s=10,
            params={"rps": 5, "users": 2, "time": 30},
        )


def test_run_without_a_target_explains_what_is_missing():
    args = build_parser().parse_args(["run"])
    with pytest.raises(SystemExit, match="--target"):
        _experiment_from_args(args)


def test_a_missing_plan_file_is_reported_clearly(tmp_path):
    args = build_parser().parse_args(["run", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit, match="not found"):
        _experiment_from_args(args)
