"""The upstream application source, pinned, for code recommendations.

The images in app/docker-compose.yml are built from GoogleCloudPlatform/microservices-demo at the
tag and commit recorded in docs/services.yaml (`source:`). A code recommendation is written against
exactly that source: the agent sees real functions from it, its edits must match it verbatim, and
the patch it produces applies to it with `git apply`.

Nothing here touches the running stack. The checkout lives under .cache/ (git-ignored), and the
compile check builds a scratch copy inside the Go image the service's own Dockerfile uses.
"""

from __future__ import annotations

import difflib
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from safety.limits import REPO_ROOT

from .models import BuildCheck, CodeEdit

CHECKOUT_DIR = REPO_ROOT / ".cache" / "upstream" / "microservices-demo"
#: Docker volume that keeps downloaded Go modules between compile checks.
GO_MODULE_CACHE = "remediation-gomod"


class SourceError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None, timeout: float = 300) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        raise SourceError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout.strip()


def ensure_checkout(source: dict[str, Any], path: Path = CHECKOUT_DIR) -> Path:
    """The pinned upstream checkout, cloned on first use. Refuses a checkout at any other commit."""
    commit = source["commit"]
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(
            "-c", "core.autocrlf=false",  # LF as upstream has it, or the patch would not apply
            "clone", "--depth", "1", "--branch", source["tag"],
            f"https://github.com/{source['repo']}.git", str(path),
        )
    head = _git("rev-parse", "HEAD", cwd=path)
    if head != commit:
        raise SourceError(f"{path} is at {head[:10]}, not the pinned {commit[:10]} ({source['tag']})")
    if _git("status", "--porcelain", cwd=path):
        raise SourceError(f"{path} has local modifications; edits must match the pristine source")
    return path


# ---------------------------------------------------------------- reading
def _func_pattern(name: str) -> re.Pattern[str]:
    # Top-level Go function or method: `func name(` or `func (recv *T) name(`.
    return re.compile(rf"^func (\([^)]*\) )?{re.escape(name)}\(")


def go_function(text: str, name: str) -> tuple[int, str]:
    """(first line number, source) of a top-level Go function. gofmt puts its closing brace in column 0."""
    lines = text.splitlines()
    pattern = _func_pattern(name)
    for i, line in enumerate(lines):
        if pattern.match(line):
            for j in range(i, len(lines)):
                if lines[j] == "}":
                    return i + 1, "\n".join(lines[i : j + 1])
            break
    raise SourceError(f"no top-level function {name!r}")


def go_imports(text: str) -> str:
    match = re.search(r"^import \($.*?^\)$", text, flags=re.MULTILINE | re.DOTALL)
    return match.group(0) if match else ""


def family(language: str) -> str:
    """How a language's functions are delimited: 'go', 'python' (indentation) or 'brace'."""
    lang = language.lower()
    return "go" if lang.startswith("go") else "python" if lang.startswith("python") else "brace"


def python_function(text: str, name: str) -> tuple[int, str]:
    """A `def` (or `Class.method`) and every line indented under it."""
    lines = text.splitlines()
    cls, _, func = name.rpartition(".")
    start, floor = 0, -1
    if cls:
        for i, line in enumerate(lines):
            if re.match(rf"^(\s*)class {re.escape(cls)}\b", line):
                start, floor = i + 1, len(line) - len(line.lstrip())
                break
        else:
            raise SourceError(f"no class {cls!r}")
    pattern = re.compile(rf"^(\s*)(async\s+)?def\s+{re.escape(func)}\s*\(")
    for i in range(start, len(lines)):
        indent = len(lines[i]) - len(lines[i].lstrip())
        if cls and lines[i].strip() and indent <= floor:
            break  # left the class
        match = pattern.match(lines[i])
        if match:
            end = i + 1
            while end < len(lines) and (
                not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > len(match.group(1))
            ):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return i + 1, "\n".join(lines[i:end])
    raise SourceError(f"no function {name!r}")


def brace_function(text: str, name: str) -> tuple[int, str]:
    """A method or function in C#, Java or JavaScript: its declaration through the matching brace.

    A declaration is `function name(`, or a line naming `name(` with nothing call-like before it:
    no `(`, `=` or `;`, and no `.` right before the name (`Task<Hipstershop.Cart> GetCartAsync(` is
    a declaration, `_cartStore.GetCartAsync(` a call). Braces are counted naively; the upstream
    sources keep them out of strings.
    """
    lines = text.splitlines()
    pattern = re.compile(rf"(\bfunction\s+)?\b{re.escape(name)}\s*\(")
    for i, line in enumerate(lines):
        match = pattern.search(line)
        if not match or line.lstrip().startswith(("//", "*", "return ", "new ", "await ")):
            continue
        before = line[: match.start()]
        if not match.group(1) and (any(c in before for c in "(=;") or before.rstrip().endswith(".")):
            continue
        depth, opened = 0, False
        for j in range(i, len(lines)):
            code = lines[j] if j > i else lines[j][match.start() :]
            if not opened and ";" in code.split("{")[0] and j > i:
                break  # a call statement, not a declaration
            depth += code.count("{") - code.count("}")
            opened = opened or "{" in code
            if opened and depth <= 0:
                return i + 1, "\n".join(lines[i : j + 1])
    raise SourceError(f"no function {name!r}")


def find_function(text: str, name: str, language: str) -> tuple[int, str]:
    """(first line number, source) of the named function or method in a file of this language."""
    kind = family(language)
    if kind == "go":
        return go_function(text, name)
    return python_function(text, name) if kind == "python" else brace_function(text, name)


IMPORT_LINE = {
    "python": re.compile(r"^(import |from \S+ import )"),
    "brace": re.compile(r"^(using [\w.]+;|import [\w.*]+;|(const|let|var) .*=\s*require\(|import .* from )"),
}


def file_imports(text: str, language: str) -> str:
    """The lines a new dependency would be added to: Go's import block, or the import/using lines."""
    kind = family(language)
    if kind == "go":
        return go_imports(text)
    return "\n".join(line for line in text.splitlines() if IMPORT_LINE[kind].match(line))


# ---------------------------------------------------------------- editing
def resolve_file(code_dir: str, file: str) -> str | None:
    """`file` as a repo-relative POSIX path inside the service's directory, or None if it escapes."""
    rel = PurePosixPath(file.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    return str(PurePosixPath(code_dir) / rel)


@dataclass
class EditResult:
    files: dict[str, tuple[str, str]]  # repo-relative path -> (original text, edited text)
    problems: list[str]

    @property
    def changed_lines(self) -> int:
        total = 0
        for old, new in self.files.values():
            for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
                if line[:1] in "+-" and not line.startswith(("+++", "---")):
                    total += 1
        return total


def _scope(text: str, function: str, language: str = "Go") -> tuple[int, int]:
    """Character span of the named function in `text`, or the whole text if none is named."""
    if not function:
        return 0, len(text)
    line, body = find_function(text, function, language)
    start = sum(len(x) for x in text.splitlines(keepends=True)[: line - 1])
    return start, start + len(body)


def find_matches(text: str, original: str, forgive_indent: bool = True) -> list[tuple[int, int, bool]]:
    """Spans where `original` occurs: verbatim, or else as whole lines compared without indentation.

    Models copy code faithfully but not its whitespace (a dropped indent, an extra tab). Comparing
    stripped lines still demands every line match, in order; only the indentation is forgiven, and
    gofmt restores it before anything is compiled. Python gets no such leniency: there indentation
    is syntax and nothing reformats it. The flag says whether the match was line-based.
    """
    exact, at = [], text.find(original)
    while at != -1:
        exact.append((at, at + len(original), False))
        at = text.find(original, at + 1)
    if exact or not forgive_indent:
        return exact
    wanted = [line.strip() for line in original.strip("\n").splitlines()]
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return [
        (offsets[i], offsets[i + len(wanted)], True)
        for i in range(len(lines) - len(wanted) + 1)
        if all(lines[i + k].strip() == wanted[k] for k in range(len(wanted)))
    ]


def _splice(text: str, start: int, end: int, replacement: str, by_line: bool) -> str:
    if not by_line:
        return text[:start] + replacement + text[end:]
    # Whole lines were matched: keep the source's indentation on the first line if the model
    # dropped it (it usually quotes from the first non-blank character), and end with a newline.
    first = text[start:end].splitlines()[0]
    indent = first[: len(first) - len(first.lstrip())]
    new = replacement.strip("\n")
    if new and not new[0].isspace():
        new = indent + new
    return text[:start] + new + "\n" + text[end:]


def apply_edits(
    root: Path,
    code_dirs: dict[str, str],
    edits: list[CodeEdit],
    languages: dict[str, str] | None = None,
) -> EditResult:
    """Apply each edit in order. `original` must match exactly once in its function (or file)."""
    files: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for n, edit in enumerate(edits, 1):
        where = f"edit {n} ({edit.service}/{edit.file})"
        code_dir = code_dirs.get(edit.service)
        language = (languages or {}).get(edit.service, "Go")
        if code_dir is None:
            problems.append(f"{where}: no source mapped for {edit.service} in docs/services.yaml")
            continue
        path = resolve_file(code_dir, edit.file)
        if path is None:
            problems.append(f"{where}: the file must be a relative path inside {code_dir}")
            continue
        if path not in files:
            full = root / path
            if not full.is_file():
                problems.append(f"{where}: {path} does not exist in the pinned source")
                continue
            text = full.read_text(encoding="utf-8")
            files[path] = (text, text)
        old, current = files[path]
        if not edit.original.strip():
            problems.append(f"{where}: original is empty - quote the exact code to replace")
            continue
        if edit.original == edit.replacement:
            problems.append(f"{where}: replacement is identical to original")
            continue
        try:
            lo, hi = _scope(current, edit.function, language)
        except SourceError:
            problems.append(f"{where}: no function {edit.function!r} in {path}")
            continue
        matches = find_matches(current[lo:hi], edit.original, family(language) != "python")
        if len(matches) != 1:
            scope = f"function {edit.function}" if edit.function else path
            problems.append(
                f"{where}: original text matches {len(matches)} times in {scope}; it must match exactly once"
            )
            continue
        start, end, by_line = matches[0]
        files[path] = (old, _splice(current, lo + start, lo + end, edit.replacement, by_line))
    return EditResult({p: t for p, t in files.items() if t[0] != t[1]}, problems)


def render_patch(files: dict[str, tuple[str, str]]) -> str:
    """A git-style unified diff (a/ b/ prefixes) that `git apply` accepts at the upstream root."""
    chunks = []
    for path in sorted(files):
        old, new = files[path]
        chunks.append(
            f"diff --git a/{path} b/{path}\n"
            + "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
                )
            )
        )
    return "".join(chunks)


def drop_format_only(original: str, formatted_original: str, formatted_edit: str) -> str:
    """`formatted_edit` minus the reformatting gofmt would do to the untouched upstream file anyway.

    Upstream is not entirely gofmt-clean, so formatting the edited file also rewrites lines nobody
    edited. Any change that is exactly gofmt's change to the pristine file is put back, so the
    patch holds the recommendation and nothing else. Whitespace only: the code that compiled is
    the same code.
    """
    old, fmt_old, new = (t.splitlines(keepends=True) for t in (original, formatted_original, formatted_edit))
    gofmt_only = {
        ("".join(old[i1:i2]), "".join(fmt_old[j1:j2]))
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, fmt_old, autojunk=False).get_opcodes()
        if tag != "equal"
    }
    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        before, after = "".join(old[i1:i2]), "".join(new[j1:j2])
        out.append(before if tag != "equal" and (before, after) in gofmt_only else after)
    return "".join(out)


# ---------------------------------------------------------------- compile check
def builder_image(dockerfile: Path) -> str | None:
    """The image of the Dockerfile's first stage (`FROM [--platform=..] image [AS name]`)."""
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        parts = [p for p in line.split() if not p.startswith("--")]
        if parts and parts[0].upper() == "FROM" and len(parts) > 1:
            return parts[1]
    return None


def check_script(language: str, names: list[str]) -> tuple[str, str] | None:
    """(shell script, what it proves) for the edited files of one service, or None if unsupported.

    Go, C# (`dotnet build`) and Java (`gradle compileJava`) are fully compiled; Go is also
    formatted. Node and Python get a syntax check of each edited file
    (`node --check`, `py_compile`): it catches what a model typically breaks - a brace, an indent,
    a stray token - but not a misspelt name or a wrong argument, which only running the code would.
    """
    lang, quoted = language.lower(), " ".join(shlex.quote(n) for n in names)
    if lang.startswith("go"):
        script = f"gofmt -w {quoted} && cd _pristine && gofmt -w {quoted} && cd .. && go build -o /dev/null ."
        return script, "gofmt and go build"
    if lang.startswith("node"):
        js = [shlex.quote(n) for n in names if n.endswith((".js", ".cjs", ".mjs"))]
        return (f'for f in {" ".join(js)}; do node --check "$f" || exit 1; done' if js else "true"), (
            "syntax check (node --check)"
        )
    if lang.startswith("python"):
        py = " ".join(shlex.quote(n) for n in names if n.endswith(".py"))
        return (f"python -m py_compile {py}" if py else "true"), "syntax check (python -m py_compile)"
    if lang.startswith("c#"):
        # A full compile of the project, as the Dockerfile's `dotnet publish` does (minus trimming).
        return "dotnet build *.csproj -nologo -v q -clp:ErrorsOnly", "dotnet build"
    if lang.startswith("java"):
        script = "chmod +x gradlew && ./gradlew compileJava --no-daemon -q --console=plain"
        return script, "gradle compileJava"
    return None


#: Package caches kept between build checks, per language, so only the first check downloads.
BUILD_CACHES = {
    "go": [(GO_MODULE_CACHE, "/go/pkg/mod")],
    "c#": [("remediation-nuget", "/root/.nuget/packages")],
    "java": [("remediation-gradle", "/root/.gradle")],
}


def build_caches(language: str) -> list[tuple[str, str]]:
    lang = language.lower()
    return next((mounts for key, mounts in BUILD_CACHES.items() if lang.startswith(key)), [])


def build_check(
    root: Path, code_dir: str, language: str, files: dict[str, tuple[str, str]], timeout: float = 900
) -> tuple[BuildCheck, dict[str, tuple[str, str]]]:
    """Check the edited service in the image its own Dockerfile builds with (its first stage).

    Go: gofmt, then go build; the files come back as formatted, minus gofmt's fixes to lines nobody
    edited, and the patch is rendered from them, so it shows exactly what compiled. Node and Python:
    a syntax check of the edited files. C# and Java: a compile of the whole project. Files other
    than Go come back unchanged; other languages are skipped.
    """
    rel = {path: str(PurePosixPath(path).relative_to(code_dir)) for path in files}
    plan = check_script(language, sorted(rel.values()))
    if plan is None:
        return BuildCheck(status="skipped", detail=f"no build check for {language} services"), files
    script, what = plan
    image = builder_image(root / code_dir / "Dockerfile")
    if image is None:
        return BuildCheck(status="skipped", detail=f"no FROM line in {code_dir}/Dockerfile"), files
    go = family(language) == "go"
    with tempfile.TemporaryDirectory(prefix="remediation-build-") as tmp:
        work = Path(tmp) / "src"
        shutil.copytree(root / code_dir, work)
        pristine = work / "_pristine"  # `go build .` ignores it; gofmt shows what upstream itself fails
        for path, (old, new) in files.items():
            (work / rel[path]).write_text(new, encoding="utf-8", newline="\n")
            if go:
                (pristine / rel[path]).parent.mkdir(parents=True, exist_ok=True)
                (pristine / rel[path]).write_text(old, encoding="utf-8", newline="\n")
        cmd = ["docker", "run", "--rm", "-v", f"{work}:/src", "-w", "/src"]
        if go:  # build with the image's Go, as the Dockerfile does
            cmd += ["-e", "GOTOOLCHAIN=local"]
        for volume, mount in build_caches(language):
            cmd += ["-v", f"{volume}:{mount}"]
        cmd += [image, "sh", "-c", script]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError:
            return BuildCheck(status="skipped", detail="docker CLI not found"), files
        except subprocess.TimeoutExpired:
            return BuildCheck(status="skipped", detail=f"{what} did not finish in {timeout:.0f}s"), files
        checked = files
        if go:
            checked = {
                path: (
                    old,
                    drop_format_only(
                        old,
                        (pristine / rel[path]).read_text(encoding="utf-8"),
                        (work / rel[path]).read_text(encoding="utf-8"),
                    ),
                )
                for path, (old, _) in files.items()
            }
    output = (proc.stderr or proc.stdout).strip()
    if proc.returncode == 0:
        return BuildCheck(status="passed", detail=f"{what} succeeded in {image.split('@')[0]}"), checked
    if proc.returncode == 125:  # docker itself failed (pull, daemon), not the check
        return BuildCheck(status="skipped", detail=f"could not run {image}: {output[-300:]}"), files
    return BuildCheck(status="failed", detail=f"{what} failed:\n{output[-1500:]}"), checked
