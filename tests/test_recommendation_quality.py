"""Recommendation quality: one fault per request, callers' code first for outages, and mechanical
checks that an edit does what it claims. The lint cases are patterns from real recommendations."""

from __future__ import annotations

from pathlib import Path

import pytest

from remediation.agent import MinuteBudget, code_excerpts, fault_groups
from remediation.codelint import lint_edits
from remediation.models import CodeEdit
from remediation.policy import check_proposal
from remediation.recommend import recommend_config
from remediation.store import Store
from reporter.demo import run_demo
from tests.test_remediation import make_draft, make_result, write_record

GO = {"frontend": "Go", "cartservice": "C#", "recommendationservice": "Python"}


def edit(replacement: str, original: str = "\treturn resp.GetItems(), err\n", **kw) -> CodeEdit:
    fields = {"service": "frontend", "file": "rpc.go", "function": "getCart", "purpose": "x"}
    return CodeEdit(**{**fields, **kw}, original=original, replacement=replacement)


# ---------------------------------------------------------------- codelint
def test_a_fallback_that_logs_is_clean():  # rem-20261010-225614, accepted
    fallback = edit(
        "\tif err != nil {\n\t\tlog.WithError(err).Warn(\"cart unavailable\")\n\t\tcart = nil\n\t}\n"
    )
    assert lint_edits([fallback], GO, "cart errors are treated as fatal") == []


def test_a_silent_empty_cart_is_caught():  # rem-20261010-231703
    silent = edit(
        "\tif err != nil {\n\t\t// Return an empty cart on error so the page can still render.\n"
        "\t\treturn []*pb.CartItem{}, nil\n\t}\n\treturn resp.GetItems(), nil\n"
    )
    (problem,) = lint_edits([silent], GO, "cart errors are fatal")
    assert "neither logs nor returns" in problem


def test_the_last_live_patch_fails_three_ways():
    go = edit(
        "\tif err != nil {\n\t\t// Log and tolerate catalog failure by returning an empty product list\n"
        "\t\treturn []*pb.Product{}, nil\n\t}\n",
        function="getProducts",
    )
    cs = edit(
        "catch (Exception ex)\n{\n    Console.WriteLine($\"Redis error: {ex}\");\n"
        "    return new Hipstershop.Cart();\n}\n",
        original="catch (Exception ex)\n{\n"
        "    throw new RpcException(new Status(StatusCode.FailedPrecondition, ex.Message));\n}\n",
        service="cartservice",
        file="cartstore/RedisCartStore.cs",
        function="GetCartAsync",
    )
    diagnosis = "no per-call deadlines cause hangs when productcatalogservice is paused"
    problems = lint_edits([go, cs], GO, diagnosis)
    assert any("neither logs nor returns" in p for p in problems)
    assert any("comment says the error is logged" in p for p in problems)
    assert any("no edit adds one" in p for p in problems)
    assert not any("cartservice" in p for p in problems)  # the C# branch does log


def test_a_deadline_needs_its_cancel_called():  # rem-20261010-230041 did it right
    right = edit("\tctx, cancel := context.WithTimeout(ctx, 2*time.Second)\n\tdefer cancel()\n")
    leaky = edit("\tctx, _ := context.WithTimeout(ctx, 2*time.Second)\n")
    diagnosis = "missing per-call deadlines on backend gRPC calls"
    assert lint_edits([right], GO, diagnosis) == []
    assert any("cancel func" in p for p in lint_edits([leaky], GO, diagnosis))


def test_upstream_branches_the_edit_left_alone_are_not_judged():
    branch = "\tif err != nil {\n\t\treturn nil\n\t}\n"
    untouched = edit(branch + "\tx := 1\n", original=branch)
    assert lint_edits([untouched], GO, "") == []


def test_python_except_pass_is_caught():
    swallow = edit(
        "    try:\n        resp = stub.ListProducts(req)\n    except Exception:\n        return []\n",
        original="    resp = stub.ListProducts(req)\n",
        service="recommendationservice", file="recommendation_server.py", function="ListRecommendations",
    )
    assert any("neither logs" in p for p in lint_edits([swallow], GO, ""))


def test_symptoms_that_mention_timeouts_do_not_demand_a_deadline():
    fallback = edit("\tif err != nil {\n\t\tlog.Warn(err)\n\t\tcart = nil\n\t}\n")
    assert lint_edits([fallback], GO, "the cart page timed out and returned HTTP 500") == []


# ---------------------------------------------------------------- synthetic data
def test_a_proposal_built_on_demo_data_cannot_be_approved(results_dir, tmp_path, config):
    reports = tmp_path / "reports"
    run_demo("kill", results_dir, reports)
    draft = make_draft(evidence=[make_draft().evidence[0].model_copy(update={
        "result_id": "demo-kill-redis-cart", "field": "experiment.action", "observed": "kill",
    })])
    problems = check_proposal(draft, {"cartservice": {}}, config, reports)
    assert any("synthetic data" in p for p in problems)
    assert not any("synthetic" in p for p in check_proposal(draft, {"cartservice": {}}, config, tmp_path))


# ---------------------------------------------------------------- one fault per request
def test_records_are_grouped_by_fault_most_recent_first():
    a1, b, a2 = make_result("kill-cart-2"), make_result("pause-cat-1"), make_result("kill-cart-1")
    b.experiment.action, b.service = "pause", "productcatalogservice"
    assert [[r.experiment.id for r in g] for g in fault_groups([a1, b, a2])] == [
        ["kill-cart-2", "kill-cart-1"],
        ["pause-cat-1"],
    ]


class CountingProposer:
    model_name = "fake"

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def propose(self, context: str):
        self.contexts.append(context)
        return make_draft(remediable=False, changes=[], declined_reason="code only")


def test_each_fault_gets_its_own_request_and_proposal(results_dir, config, write_telemetry):
    write_telemetry()
    for exp_id, action, service in (("kill-cart-1", "kill", "cartservice"), ("pause-cat-1", "pause", "x")):
        record = make_result(exp_id)
        record.experiment.action, record.service = action, service
        write_record(results_dir, record)
    proposer = CountingProposer()
    filed = recommend_config(proposer, config, {}, ["kill-cart-1", "pause-cat-1"])
    assert len(proposer.contexts) == 2 and len(filed.proposals) == 2
    assert [p.source_results for p in filed.proposals] == [["kill-cart-1"], ["pause-cat-1"]]
    assert "## pause-cat-1" not in proposer.contexts[0]  # no fault sees the other's records
    assert len(Store(results_dir).ids()) == 2


def test_unnamed_runs_answer_only_the_latest_faults(results_dir, config, write_telemetry):
    write_telemetry()
    for i in range(4):
        record = make_result(f"run-{i}")
        record.service = f"svc{i}"
        write_record(results_dir, record)
    filed = recommend_config(CountingProposer(), config, {}, max_faults=2)
    assert len(filed.proposals) == 2 and len(filed.skipped) == 2


def test_the_minute_budget_waits_instead_of_being_refused():
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    budget = MinuteBudget(limit=8000, clock=lambda: now[0], sleep=sleep)
    budget.record(6000)
    now[0] = 10.0
    assert budget.wait_for(1500) == 0  # 7,500 fits
    assert budget.wait_for(2500) > 0 and now[0] >= 60  # 8,500 would not: wait out the first request
    assert budget.used() == 0


# ---------------------------------------------------------------- what code is shown first
DOCS = {
    "productcatalogservice": {
        "language": "Go",
        "code": {
            "dir": "src/catalog",
            "serves": {"ListProducts": {"file": "server.go", "functions": ["ListProducts"]}},
        },
    },
    "frontend": {
        "language": "Go",
        "calls": {"GET /": {"fatal": ["productcatalogservice"]}},
        "code": {
            "dir": "src/frontend",
            "calls": {"productcatalogservice": {"file": "rpc.go", "functions": ["getProducts"]}},
        },
    },
}


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "upstream"
    (root / "src" / "catalog").mkdir(parents=True)
    (root / "src" / "frontend").mkdir(parents=True)
    body = "".join(f"\tx{i} := {i}\n" for i in range(40))
    (root / "src" / "catalog" / "server.go").write_text(
        f"package main\n\nfunc (p *catalog) ListProducts() {{\n{body}}}\n", encoding="utf-8", newline="\n"
    )
    (root / "src" / "frontend" / "rpc.go").write_text(
        "package main\n\n"
        "func (fe *frontendServer) getProducts(ctx context.Context) error {\n\treturn nil\n}\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


@pytest.mark.parametrize(("action", "first"), [("pause", "getProducts"), ("spike", "ListProducts")])
def test_outages_show_the_callers_first(checkout, action, first):
    record = make_result("r-1")
    record.experiment.target = record.service = "productcatalogservice"
    record.experiment.action = action
    full, _ = code_excerpts([record], DOCS, checkout)
    # Room for one function only: which one wins depends on the fault.
    smaller = min(len(full) - 60, 900)
    text, omitted = code_excerpts([record], DOCS, checkout, max_chars=smaller)
    assert f"({first})" in text and len(omitted) == 1
