"""Code recommendations: source excerpts, exact-edit matching, the patch, policy and the review gate."""

from __future__ import annotations

import copy
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from remediation.__main__ import file_code_proposal
from remediation.agent import code_excerpts, experiment_records
from remediation.models import (
    BuildCheck,
    CodeEdit,
    CodeProposalDraft,
    Critique,
    Evidence,
    SuccessCriterion,
    ValidationExperiment,
)
from remediation.policy import PROTECTED_SERVICES, SERVICE_DOCS, check_code_proposal
from remediation.recommend import review_and_revise
from remediation.source import (
    CHECKOUT_DIR,
    SourceError,
    apply_edits,
    build_caches,
    build_check,
    check_script,
    drop_format_only,
    ensure_checkout,
    file_imports,
    find_function,
    find_matches,
    go_function,
    go_imports,
    render_patch,
)
from remediation.store import Store
from tests.test_remediation import make_proposal, make_result, write_record

HANDLERS = """package main

import (
\t"net/http"
)

func (fe *frontendServer) homeHandler(w http.ResponseWriter, r *http.Request) {
\tcart, err := fe.getCart(r.Context(), sessionID(r))
\tif err != nil {
\t\trenderHTTPError(log, r, w, err, http.StatusInternalServerError)
\t\treturn
\t}
\t_ = cart
}

func (fe *frontendServer) productHandler(w http.ResponseWriter, r *http.Request) {
\tcart, err := fe.getCart(r.Context(), sessionID(r))
\tif err != nil {
\t\trenderHTTPError(log, r, w, err, http.StatusInternalServerError)
\t\treturn
\t}
\t_ = cart
}
"""
RPC = """package main

func (fe *frontendServer) getCart(ctx context.Context, userID string) ([]*pb.CartItem, error) {
\tresp, err := pb.NewCartServiceClient(fe.cartSvcConn).GetCart(ctx, &pb.GetCartRequest{UserId: userID})
\treturn resp.GetItems(), err
}

func (fe *frontendServer) getAd(ctx context.Context, ctxKeys []string) ([]*pb.Ad, error) {
\treturn nil, nil
}
"""
DOCS = {
    "frontend": {
        "language": "Go",
        "calls": {"GET /": {"fatal": ["cartservice"]}},
        "code": {
            "dir": "src/frontend",
            "calls": {
                "cartservice": {"file": "rpc.go", "functions": ["getCart"]},
                "adservice": {"file": "rpc.go", "functions": ["getAd"]},
            },
            "pages": {"Cart": {"file": "handlers.go", "functions": ["productHandler"]}},
        },
    },
    "cartservice": {"language": "C#", "calls": {"redis-cart": "every read"}},
    "redis-cart": {"language": "Redis", "protected": True},
}
FALLBACK = (
    "\t\tlog.WithError(err).Warn(\"no cart\")\n\t\tcart = nil\n"
)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "upstream"
    (root / "src" / "frontend").mkdir(parents=True)
    (root / "src" / "frontend" / "handlers.go").write_text(HANDLERS, encoding="utf-8", newline="\n")
    (root / "src" / "frontend" / "rpc.go").write_text(RPC, encoding="utf-8", newline="\n")
    (root / "src" / "frontend" / "Dockerfile").write_text("FROM golang:1.27-alpine AS builder\n")
    return root


def fallback_edit(function: str = "homeHandler", original: str | None = None) -> CodeEdit:
    return CodeEdit(
        service="frontend",
        file="handlers.go",
        function=function,
        original=original
        or "\t\trenderHTTPError(log, r, w, err, http.StatusInternalServerError)\n\t\treturn\n",
        replacement=FALLBACK,
        purpose="render the page with an empty cart instead of HTTP 500",
    )


def make_code_draft(**overrides) -> CodeProposalDraft:
    fields = {
        "remediable": True,
        "diagnosed_problem": "a cart outage fails the home page",
        "evidence": [
            Evidence(result_id="kill-cart-1", field="recovery_seconds", observed="10.0", interpretation="x")
        ],
        "edits": [fallback_edit()],
        "expected_benefit": "home renders while the cart is down",
        "success_criteria": [SuccessCriterion(metric="during_error_rate_max", min_improvement_pct=50)],
        "side_effects": ["the cart badge shows 0 during an outage"],
        "validation_experiment": ValidationExperiment(target="redis-cart", action="stop", duration_s=20),
    }
    fields.update(overrides)
    return CodeProposalDraft(**fields)


# ---------------------------------------------------------------- reading the source
def test_a_go_function_is_extracted_whole_with_its_line_number():
    line, body = go_function(HANDLERS, "productHandler")
    assert line == 16
    assert body.startswith("func (fe *frontendServer) productHandler(") and body.endswith("\n}")
    assert go_imports(HANDLERS).startswith("import (") and go_imports(HANDLERS).endswith(")")
    with pytest.raises(SourceError):
        go_function(HANDLERS, "nope")


def test_excerpts_show_the_faulted_call_sites_and_failed_pages(checkout):
    record = make_result("kill-redis-1")
    record.experiment.target = record.service = "redis-cart"
    text, omitted = code_excerpts([record], DOCS, checkout)
    # redis-cart is reached through cartservice, so the frontend's cartservice calls are shown,
    # and Cart failed, so its handler is shown - nothing unrelated.
    assert "getCart(" in text and "productHandler(" in text
    assert "getAd(" not in text and "homeHandler(" not in text and not omitted


def test_with_no_faulted_call_or_failed_page_every_call_site_is_shown(checkout):
    record = make_result("spike-1", err_max=0)
    record.experiment.target = record.service = "frontend"
    text, _ = code_excerpts([record], DOCS, checkout)
    assert "getCart(" in text and "getAd(" in text


# ---------------------------------------------------------------- matching edits
def test_an_edit_scoped_to_a_function_resolves_identical_snippets(checkout):
    result = apply_edits(checkout, {"frontend": "src/frontend"}, [fallback_edit("productHandler")])
    assert not result.problems
    new = result.files["src/frontend/handlers.go"][1]
    assert new.count("renderHTTPError") == 1  # homeHandler untouched
    assert go_function(new, "productHandler")[1].count("no cart") == 1


def test_an_unscoped_snippet_that_occurs_twice_is_refused(checkout):
    result = apply_edits(checkout, {"frontend": "src/frontend"}, [fallback_edit(function="")])
    assert "matches 2 times" in result.problems[0] and not result.files


def test_indentation_is_forgiven_but_every_line_must_still_match(checkout):
    sloppy = "renderHTTPError(log, r, w, err, http.StatusInternalServerError)\n\treturn\n\t\t}"
    ok = apply_edits(checkout, {"frontend": "src/frontend"}, [fallback_edit(original=sloppy)])
    assert not ok.problems
    homeHandler = go_function(ok.files["src/frontend/handlers.go"][1], "homeHandler")[1]
    assert "\t\tlog.WithError" in homeHandler and "renderHTTPError" not in homeHandler
    wrong = sloppy.replace("return", "return nil")
    bad = apply_edits(checkout, {"frontend": "src/frontend"}, [fallback_edit(original=wrong)])
    assert "matches 0 times" in bad.problems[0]
    assert find_matches("a\nb\n", "a\nc") == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        ({"file": "../../etc/passwd"}, "relative path inside"),
        ({"file": "missing.go"}, "does not exist"),
        ({"service": "cartservice"}, "no source mapped"),
        ({"function": "nope"}, "no function"),
        ({"replacement": "\t\trenderHTTPError(log, r, w, err, http.StatusInternalServerError)\n\t\treturn\n"},
         "identical"),
    ],
)
def test_bad_edits_are_named_precisely(checkout, edit, expected):
    bad = fallback_edit().model_copy(update=edit)
    result = apply_edits(checkout, {"frontend": "src/frontend"}, [bad])
    assert expected in result.problems[0]


def test_the_patch_applies_to_the_pristine_source_with_git(checkout):
    result = apply_edits(checkout, {"frontend": "src/frontend"}, [fallback_edit()])
    patch = render_patch(result.files)
    assert patch.startswith("diff --git a/src/frontend/handlers.go b/src/frontend/handlers.go")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    (checkout / "fix.diff").write_text(patch, encoding="utf-8", newline="\n")
    proc = subprocess.run(
        ["git", "apply", "--check", "fix.diff"], cwd=checkout, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_gofmt_changes_upstream_would_get_anyway_stay_out_of_the_patch():
    original = "a\nx + y\nb\nkeep\n"
    assert drop_format_only(original, "a\nx+y\nb\nkeep\n", "a\nx+y\nb\nchanged\n") == "a\nx + y\nb\nchanged\n"


def test_each_language_gets_the_check_its_toolchain_can_do(checkout):
    go_script, go_what = check_script("Go", ["handlers.go"])
    assert "gofmt -w handlers.go" in go_script and go_script.endswith("go build -o /dev/null .")
    node_script, node_what = check_script("Node.js", ["server.js", "package.json"])
    assert 'for f in server.js; do node --check "$f"' in node_script and "syntax check" in node_what
    py_script, _ = check_script("Python", ["email_server.py", "templates/x.html"])
    assert py_script == "python -m py_compile email_server.py"
    assert check_script("Python", ["templates/x.html"])[0] == "true"  # nothing to check is a pass
    assert check_script("Go", ["a b;rm -rf /.go"])[0].startswith("gofmt -w 'a b;rm -rf /.go'")
    # C# and Java get a full compile of the project, with their package caches kept between runs.
    cs_script, cs_what = check_script("C# (.NET, ASP.NET Core gRPC)", ["cartstore/RedisCartStore.cs"])
    assert cs_script.startswith("dotnet build *.csproj") and cs_what == "dotnet build"
    java_script, _ = check_script("Java", ["src/main/java/hipstershop/AdService.java"])
    assert "./gradlew compileJava" in java_script
    assert build_caches("C#")[0][1] == "/root/.nuget/packages" and build_caches("Java")
    # Anything else is skipped, files returned untouched (and no container is started).
    files = {"src/frontend/rpc.go": ("a", "b")}
    check, back = build_check(checkout, "src/frontend", "Rust", files)
    assert check.status == "skipped" and back == files


def test_a_checkout_at_another_commit_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                                 "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    with pytest.raises(SourceError, match="not the pinned"):
        ensure_checkout({"repo": "r/r", "tag": "v1", "commit": "0" * 40}, repo)


# ---------------------------------------------------------------- policy, filing and review
def test_policy_rejects_protected_services_and_oversized_changes(results_dir, config, checkout):
    write_record(results_dir, make_result("kill-cart-1"))
    redis = fallback_edit().model_copy(update={"service": "redis-cart"})
    problems, _ = check_code_proposal(make_code_draft(edits=[redis]), config, checkout, DOCS)
    assert any("redis-cart is protected" in p for p in problems)
    huge = fallback_edit().model_copy(update={"replacement": "\t\t_ = 1\n" * 200})
    problems, _ = check_code_proposal(make_code_draft(edits=[huge]), config, checkout, DOCS)
    assert any("changed lines; at most" in p for p in problems)


def test_a_draft_that_does_not_compile_cannot_be_accepted(results_dir, config, checkout):
    write_record(results_dir, make_result("kill-cart-1"))
    declined = make_proposal(pid="rem-declined")

    def fails(_root, _dir, _lang, files):
        return BuildCheck(status="failed", detail="undefined: log"), files

    def formats(_root, _dir, _lang, files):
        return BuildCheck(status="passed"), {p: (old, new + "// fmt\n") for p, (old, new) in files.items()}

    docs_raw = {"services": DOCS, "source": {"commit": "abc"}}
    broken = file_code_proposal(make_code_draft(), "human", declined, config, checkout, docs_raw, fails)
    assert broken.policy_problems == [
        "the patched frontend fails its build check (output in the review sheet)"
    ]
    good = file_code_proposal(make_code_draft(), "human", declined, config, checkout, docs_raw, formats)
    assert not good.policy_problems and good.build.status == "passed"
    assert "+// fmt" in good.patch  # the patch is rendered from what compiled
    assert good.responds_to == "rem-declined" and good.source_commit == "abc"


def test_editing_the_patch_after_acceptance_voids_it(results_dir, config, checkout):
    write_record(results_dir, make_result("kill-cart-1"))
    store = Store(results_dir)
    proposal = file_code_proposal(
        make_code_draft(), "human", make_proposal(pid="rem-declined"), config, checkout,
        {"services": DOCS, "source": {"commit": "abc"}}, build=None,
    )
    proposal = proposal.model_copy(update={"id": "rem-code", "created_at": datetime.now().astimezone()})
    folder = store.save_proposal(proposal, {})
    assert (folder / "patch.diff").read_text(encoding="utf-8") == proposal.patch
    assert "# Code recommendation rem-code" in (folder / "review.md").read_text(encoding="utf-8")
    assert store.load_proposal("rem-code") == proposal
    store.record_decision("rem-code", "approved", "alice")
    assert store.approval_problem("rem-code") is None
    (folder / "patch.diff").write_text(proposal.patch + "+evil\n", encoding="utf-8")
    assert "patch.diff changed after it was approved" in store.approval_problem("rem-code")
    assert store.status("rem-code") == "accepted by reviewer"


# ---------------------------------------------------------------- other languages
CSHARP = """using System;
using Grpc.Core;

namespace cartservice.cartstore
{
    public class RedisCartStore : ICartStore
    {
        public async Task<Hipstershop.Cart> GetCartAsync(string userId)
        {
            try
            {
                return await Load(userId);
            }
            catch (Exception ex)
            {
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"no {ex}"));
            }
        }

        public async Task AddItemAsync(string userId)
        {
            var cart = await _store.GetCartAsync(userId);
        }
    }
}
"""
PYTHON = """import grpc
from jinja2 import TemplateError

class EmailService(Base):
  def SendOrderConfirmation(self, request, context):
    try:
      send(request)
    except Error:
      return Empty()

    return Empty()

class DummyEmailService(Base):
  def SendOrderConfirmation(self, request, context):
    return Empty()
"""
JAVASCRIPT = """const grpc = require('@grpc/grpc-js');

function convert (call, callback) {
  _getCurrencyData((data) => {
    callback(null, data);
  });
}

module.exports = function charge (request) {
  return convert(request);
};

class Server {
  static ChargeServiceHandler(call, callback) {
    callback(null, charge(call.request));
  }
}
"""


def test_a_csharp_method_is_its_declaration_not_a_call_to_it():
    line, body = find_function(CSHARP, "GetCartAsync", "C# (.NET)")
    assert line == 8 and body.strip().endswith("}") and "catch (Exception ex)" in body
    assert find_function(CSHARP, "AddItemAsync", "C# (.NET)")[0] == 20
    # Where a name only appears as a call (`_store.GetCartAsync(`), it is not taken for a declaration.
    call_only = CSHARP[CSHARP.index("        public async Task AddItemAsync") :]
    with pytest.raises(SourceError):
        find_function(call_only, "GetCartAsync", "C# (.NET)")
    assert file_imports(CSHARP, "C# (.NET)") == "using System;\nusing Grpc.Core;"


def test_a_python_method_can_be_named_through_its_class():
    line, body = find_function(PYTHON, "EmailService.SendOrderConfirmation", "Python")
    assert line == 5 and "send(request)" in body and body.endswith("    return Empty()")
    line, body = find_function(PYTHON, "DummyEmailService.SendOrderConfirmation", "Python")
    assert line == 14 and "send(" not in body
    assert file_imports(PYTHON, "Python") == "import grpc\nfrom jinja2 import TemplateError"


def test_javascript_functions_methods_and_exported_functions_are_found():
    assert find_function(JAVASCRIPT, "convert", "Node.js")[0] == 3
    assert find_function(JAVASCRIPT, "charge", "Node.js")[1].startswith("module.exports = function charge")
    assert find_function(JAVASCRIPT, "ChargeServiceHandler", "Node.js")[1].strip().endswith("}")
    assert "require(" in file_imports(JAVASCRIPT, "Node.js")


def test_python_indentation_must_be_exact(tmp_path):
    root = tmp_path / "up"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "s.py").write_text(PYTHON, encoding="utf-8", newline="\n")
    edit = CodeEdit(
        service="email", file="s.py", function="EmailService.SendOrderConfirmation",
        original="try:\n  send(request)", replacement="try:\n  send(request, timeout=5)", purpose="x",
    )
    # Dedented quote: fine in Go (gofmt fixes it), wrong in Python, where indentation is syntax.
    result = apply_edits(root, {"email": "svc"}, [edit], {"email": "Python"})
    assert "matches 0 times" in result.problems[0]
    exact = edit.model_copy(
        update={"original": "      send(request)", "replacement": "      send(request, 5)"}
    )
    assert not apply_edits(root, {"email": "svc"}, [exact], {"email": "Python"}).problems


def test_the_frontend_reaches_a_datastore_through_one_hop_but_not_past_a_direct_call(checkout):
    docs = copy.deepcopy(DOCS)
    docs["checkoutservice"] = {
        "language": "Go",
        "calls": {"PlaceOrder": {"cartservice": "GetCart"}},
        "code": {"dir": "src/frontend", "calls": {"cartservice": {"file": "rpc.go", "functions": ["getAd"]}}},
    }
    docs["frontend"]["code"]["calls"]["checkoutservice"] = {
        "file": "handlers.go",
        "functions": ["homeHandler"],
    }
    record = make_result("kill-cart-1", err_max=0)  # target cartservice, nothing failed
    text, _ = code_excerpts([record], docs, checkout)
    # The frontend calls cartservice directly, so the detour through checkout is not shown.
    assert "getCart(" in text and "homeHandler(" not in text


# ---------------------------------------------------------------- the maps in docs/services.yaml
DOC = yaml.safe_load(SERVICE_DOCS.read_text(encoding="utf-8"))


def test_every_service_with_source_has_a_code_map():
    unmapped = {n for n, d in DOC["services"].items() if not d.get("code")}
    # redis-cart runs the stock Redis image (no source here); the load generator is protected.
    assert unmapped == {"redis-cart", "loadgenerator"} and unmapped <= set(PROTECTED_SERVICES)


@pytest.mark.skipif(not (CHECKOUT_DIR / ".git").exists(), reason="pinned upstream checkout not present")
def test_every_mapped_function_exists_in_the_pinned_source():
    missing = []
    for name, doc in DOC["services"].items():
        code = doc.get("code") or {}
        for group in ("serves", "calls", "pages"):
            for entry in (code.get(group) or {}).values():
                text = (CHECKOUT_DIR / code["dir"] / entry["file"]).read_text(encoding="utf-8")
                for function in entry["functions"]:
                    try:
                        find_function(text, function, doc["language"])
                    except SourceError:
                        missing.append(f"{name}/{entry['file']}:{function}")
    assert not missing


# ---------------------------------------------------------------- the two-key split
class _RecordingProposer:
    """A stand-in that records which stage was asked of it, so a test can see which key ran what."""

    model_name = "fake"

    def __init__(self, draft):
        self._draft = draft
        self.calls: list[str] = []

    def critique_code(self, _context):
        self.calls.append("critique")
        return Critique(fixes_diagnosis=True, issues=[])

    def propose_code(self, _context):
        self.calls.append("propose")  # a rewrite
        return self._draft


def test_the_review_runs_on_the_critic_proposer_when_one_is_given(results_dir, config, checkout):
    """With two keys the code author and the reviewer are different proposers; the critique must go to
    the reviewer (the second key's budget), never to the code author."""
    write_record(results_dir, make_result("kill-cart-1"))
    draft = make_code_draft()
    group = experiment_records(results_dir, ["kill-cart-1"])
    author, critic = _RecordingProposer(draft), _RecordingProposer(draft)

    review_and_revise(author, draft, make_proposal(pid="rem-declined"), group, config, checkout, DOCS,
                      None, critic=critic)

    assert "critique" in critic.calls            # the review ran on the critic
    assert "critique" not in author.calls         # and not on the code author
