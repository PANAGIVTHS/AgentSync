#!/usr/bin/env python3
"""agent: host-side CLI for the Claude Code agent container.

The agent never writes to your repo. You share commits through a read-only
mirror, the agent works on agent/<task> branches in its own clone, and this CLI
fetches those branches back into your repo for review and merge.

Runs on any Python 3.9+. Output uses Rich (and rich-argparse for --help) when
install.sh has set them up; otherwise it falls back to plain text.
"""
from __future__ import annotations

import argparse
import calendar
import difflib
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent" / "config.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
IMAGE = os.environ.get("AGENT_IMAGE", "agent-runtime:local")
CONTAINER = "agent"
ENTRYPOINT = "/opt/agent/bin/agent-supervisor"  # identifies our images, including old untagged ones
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
HOOK_BEGIN = "# >>> agent auto-push >>>"
HOOK_END = "# <<< agent auto-push <<<"
HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")
NO_PUSH = "no-push://disabled"
AGENT_EMAIL = "agent@localhost"  # the agent's committer identity (container/gitconfig)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]")
GB = 1024 ** 3
LOW_FREE = 5 * GB     # warn below this much free space
BUILD_NEEDS = 3 * GB  # roughly what a fresh image build takes on Docker's disk


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(**changes) -> None:
    cfg = {**load_config(), **changes}
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")


def configured_home() -> Path:
    """AGENT_HOME, else the location chosen at setup (config.json), else ~/Agent."""
    if os.environ.get("AGENT_HOME"):
        return Path(os.environ["AGENT_HOME"]).expanduser()
    if load_config().get("home"):
        return Path(load_config()["home"]).expanduser()
    return Path.home() / "Agent"


AGENT_HOME = configured_home()


class Fail(Exception):
    """A user-facing error, printed without a traceback."""


# ---------------------------------------------------------------- output
# Rich when available (installed by install.sh into the CLI's venv); plain text
# otherwise or with AGENT_PLAIN=1. Messages are passed as Text, never as markup,
# so brackets in branch names or agent-written text print literally.
#
# Rich output is themed like Claude Code: each command's output is collected
# into rounded, Claude-orange boxes (one per heading) and printed when the box
# closes: at the next heading, before a prompt or a streaming command, or at exit.

CLAUDE = "#D97757"  # Claude's terracotta orange
CRAB = (" ▐▛███▜▌ ", "▝▜█████▛▘", "  ▘▘ ▝▝  ")

try:
    if os.environ.get("AGENT_PLAIN") == "1" or sys.argv[1:2] == ["__complete"]:
        raise ImportError
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme
    THEME = Theme({"ok": "bold #4EBA65", "warn": "bold #FFC107", "bad": "bold #FF6B80", "head": f"bold {CLAUDE}",
                   "claude": CLAUDE, "muted": "#999999", "name": "bold #4EBA65", "path": "#B1B9F9",
                   "sha": "#FFC107", "cmd": f"bold {CLAUDE}", "frame": CLAUDE})
    OUT = Console(highlight=False, theme=THEME)
    ERR = Console(stderr=True, highlight=False, theme=THEME)
except ImportError:
    OUT = ERR = None

try:
    if OUT is None:
        raise ImportError
    from rich_argparse import RawDescriptionRichHelpFormatter as RichHelpFormatter

    class HELP_FORMAT(RichHelpFormatter):
        styles = {**RichHelpFormatter.styles, "argparse.groups": f"bold {CLAUDE}", "argparse.args": "#4EBA65",
                  "argparse.metavar": "#FFC107", "argparse.prog": f"bold {CLAUDE}", "argparse.syntax": f"bold {CLAUDE}",
                  "argparse.help": "default", "argparse.text": "default"}
        highlights = [*RichHelpFormatter.highlights, r"(?P<syntax>`[^`]+`)"]
except ImportError:
    HELP_FORMAT = argparse.RawDescriptionHelpFormatter

_box: list | None = None  # [title, [renderables], border style] while a box is open


def emit(renderable) -> None:
    if _box is not None:
        _box[1].append(renderable)
    else:
        OUT.print(renderable)


def close_box() -> None:
    global _box
    if _box is None or OUT is None:
        _box = None
        return
    title, items, style = _box
    _box = None
    if not items:
        items = [Text("")]
    OUT.print(Panel(Group(*items), title=title, title_align="left", border_style=style,
                    box=box.ROUNDED, padding=(0, 1), expand=False))


def open_box(title, style: str = "frame") -> None:
    global _box
    close_box()
    _box = [title, [], style]


def hang(prefix: Text, body: Text):
    """prefix + body, with wrapped lines of body indented under its first line."""
    if not prefix.plain:
        return body
    g = Table.grid()
    g.add_column(no_wrap=True)
    g.add_column(overflow="fold")
    g.add_row(prefix, body)
    return g


def _say(console, plain_prefix: str, rich_prefix: tuple[str, str], msg: str, style: str = "", err=False) -> None:
    if console is None:
        print(f"{plain_prefix}{msg}", file=sys.stderr if err else sys.stdout)
    elif _box is not None:
        emit(hang(Text(*rich_prefix), Text(msg, style)))
    else:
        console.print(hang(Text(*rich_prefix), Text(msg, style)))


def ok(msg: str) -> None:
    _say(OUT, "✓ ", ("✓ ", "ok"), msg)


def note(msg: str) -> None:
    _say(OUT, "  ", ("  ", ""), msg, "muted")


def say(msg: str, style: str = "") -> None:
    _say(OUT, "", ("", ""), msg, style)


def bullet(sign: str, msg: str, style: str = "ok") -> None:
    """A '+ thing' line in a plan; the sign is coloured."""
    _say(OUT, f"  {sign} ", (f"  {sign} ", style), msg)


def hint(label: str, command: str) -> None:
    """'  label: command', with the command highlighted."""
    if OUT is None:
        print(f"  {label}: {command}")
    else:
        emit(hang(Text("  ⎿ ", "muted"), Text.assemble((label + "  ", "muted"), (command, "cmd"))))


def warn(msg: str) -> None:
    _say(ERR, "! ", ("⚠ ", "warn"), msg, err=True)


def error(msg: str) -> None:
    if ERR is None:
        print(f"✗ {msg}", file=sys.stderr)
        return
    close_box()
    ERR.print(Panel(Text(msg), title=Text("✗ error", "bad"), title_align="left", border_style="bad",
                    box=box.ROUNDED, padding=(0, 1), expand=False))


def heading(msg: str) -> None:
    if OUT is None:
        print(msg)
    else:
        open_box(Text.assemble(("✻ ", "claude"), (msg, "bold")))


def subheading(msg: str) -> None:
    """A section title inside the current box."""
    if OUT is None:
        print(msg)
    else:
        emit(Text.assemble(("\n" if _box and _box[1] else "", ""), ("● ", "claude"), (msg, "bold")))


def banner(lines: list) -> None:
    """The crab, with up to three lines of text beside it, in a box (Claude Code's welcome look)."""
    if OUT is None:
        for line in lines:
            print(line if isinstance(line, str) else line[0])
        return
    close_box()
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="claude", no_wrap=True)
    grid.add_column()
    lines = [*lines, "", "", ""][:3]
    for crab, line in zip(CRAB, lines):
        grid.add_row(crab, Text(*(line if isinstance(line, tuple) else (line, ""))))
    OUT.print(Panel(grid, border_style="frame", box=box.ROUNDED, padding=(0, 1), expand=False))


def table(columns: list[str], rows: list[list], styles: list[str] | None = None) -> None:
    """A table; plain mode aligns columns with spaces. Cells may be (text, style) pairs."""
    cell = lambda c: c if isinstance(c, tuple) else (str(c), "")
    if OUT is None:
        rows_ = [[cell(c)[0] for c in r] for r in rows]
        widths = [max(len(r[i]) for r in [columns, *rows_]) for i in range(len(columns))]
        for r in [columns, *rows_]:
            print("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
        return
    t = Table(box=box.ROUNDED, header_style="bold", border_style="muted", pad_edge=True)
    for i, c in enumerate(columns):
        t.add_column(c, style=(styles or [""] * len(columns))[i], overflow="fold")
    for r in rows:
        t.add_row(*[Text(*cell(c)) for c in r])
    emit(t)


def kv(rows: list[list]) -> None:
    """Label/value lines with no header."""
    if OUT is None:
        w = max(len(r[0]) for r in rows)
        for label, value in rows:
            print(f"  {label.ljust(w)}   {value[0] if isinstance(value, tuple) else value}")
        return
    t = Table.grid(padding=(0, 3))
    t.add_column(style="muted")
    t.add_column()
    for label, value in rows:
        t.add_row(Text(label), Text(*(value if isinstance(value, tuple) else (str(value), ""))))
    emit(t)


def panel(text: str, title: str) -> None:
    if OUT is None:
        print(f"── {title} ──\n{text}\n")
    else:
        emit(Panel(Text(text), title=Text(title, "bold"), title_align="left", border_style="muted",
                   box=box.ROUNDED, expand=False, padding=(0, 1)))


class status:
    """A spinner while something slow runs (Rich only)."""

    def __init__(self, msg: str):
        self.s = OUT.status(Text(msg, "claude"), spinner="dots", spinner_style="claude") \
            if OUT is not None and OUT.is_terminal else None

    def __enter__(self):
        if self.s:
            self.s.__enter__()

    def __exit__(self, *exc):
        if self.s:
            self.s.__exit__(*exc)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    if OUT is None:
        answer = input(f"{question}{shown}: ")
    else:
        close_box()
        answer = OUT.input(Text.assemble(("❯ ", "claude"), (question, "bold"), (shown, "muted"), " "))
    return answer.strip() or default


def confirm(question: str, default: bool = False) -> bool:
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer in ("y", "yes")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def sanitize(text: str) -> str:
    """Agent-written text is printed to your terminal: drop escape sequences."""
    return CONTROL.sub("", text)


def clean_env() -> dict:
    # Git hooks export GIT_DIR / GIT_INDEX_FILE. Never let them leak into the
    # git commands we run against other repositories.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run(cmd, *, check=True, capture=True):
    cmd = [str(c) for c in cmd]
    if not capture:
        close_box()
    p = subprocess.run(cmd, env=clean_env(), text=True, capture_output=capture)
    if check and p.returncode:
        detail = ((p.stderr or "") + (p.stdout or "")).strip() if capture else ""
        raise Fail(f"command failed: {' '.join(cmd)}" + (f"\n{detail}" if detail else ""))
    return p


def git(repo, *args, check=True, capture=True):
    return run(["git", "-C", repo, *args], check=check, capture=capture)


def out(repo, *args) -> str:
    return git(repo, *args).stdout.strip()


# ---------------------------------------------------------------- layout

def dirs() -> dict[str, Path]:
    names = ("home", "work", "mirrors", "handoffs", "vault", "review", "tools")
    return {n: AGENT_HOME / n for n in names}


def mirror_of(name: str) -> Path:
    return AGENT_HOME / "mirrors" / f"{name}.git"


def work_of(name: str) -> Path:
    return AGENT_HOME / "work" / name


def handoff_of(name: str, task: str) -> Path:
    return AGENT_HOME / "handoffs" / name / f"{task}.md"


def review_of(name: str, task: str) -> Path:
    return AGENT_HOME / "review" / name / task


def registry_file() -> Path:
    return AGENT_HOME / "projects.json"


def load_registry() -> dict:
    f = registry_file()
    return json.loads(f.read_text()) if f.exists() else {}


def save_registry(data: dict) -> None:
    f = registry_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, f)


def resolve_project(name: str | None) -> tuple[str, Path]:
    reg = load_registry()
    if name:
        if name not in reg:
            raise Fail(f"unknown project {name!r}. Shared: {', '.join(sorted(reg)) or 'none'}")
        return name, Path(reg[name]["source"])
    cwd = Path.cwd().resolve()
    for n, info in reg.items():
        src = Path(info["source"])
        if cwd == src or src in cwd.parents:
            return n, src
    raise Fail("not inside a shared repo. Use --project NAME (see: agent list)")


def normalize_task(task: str) -> str:
    task = task.removeprefix("agent/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", task):
        raise Fail(f"invalid task name: {task!r}")
    return task


# ---------------------------------------------------------------- hooks in your repo

def hooks_dir(src: Path) -> Path | None:
    """Your repo's hooks folder, or None if core.hooksPath points elsewhere."""
    if git(src, "config", "--get", "core.hooksPath", check=False).stdout.strip():
        return None
    common = Path(out(src, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else src / common) / "hooks"


def hook_block(name: str) -> str:
    cli = shlex.quote(str(Path(__file__).resolve()))
    py = shlex.quote(sys.executable)
    return (f"{HOOK_BEGIN}\n"
            f"# Sends your commits to the agent's read-only mirror. Remove: agent unshare {name}\n"
            f"( {py} {cli} push --quiet --project {name} >/dev/null 2>&1 & )\n"
            f"{HOOK_END}\n")


def strip_block(text: str) -> str:
    return re.sub(re.escape(HOOK_BEGIN) + r".*?" + re.escape(HOOK_END) + r"\n?", "", text, flags=re.S)


def install_hooks(src: Path, name: str) -> list[str]:
    d = hooks_dir(src)
    if d is None:
        return []
    d.mkdir(parents=True, exist_ok=True)
    installed = []
    for h in HOOK_NAMES:
        f = d / h
        text = f.read_text() if f.exists() else "#!/bin/sh\n"
        first = text.splitlines()[0] if text else ""
        if f.exists() and not re.match(r"#!.*\b(sh|bash|dash|zsh)\b", first):
            warn(f"left your {h} hook alone (not a shell script); run `agent push` yourself")
            continue
        text = strip_block(text)
        if not text.endswith("\n"):
            text += "\n"
        f.write_text(text + hook_block(name))
        f.chmod(f.stat().st_mode | 0o111)
        installed.append(h)
    return installed


def remove_hooks(src: Path) -> None:
    d = hooks_dir(src)
    if d is None:
        return
    for h in HOOK_NAMES:
        f = d / h
        if not f.exists():
            continue
        text = strip_block(f.read_text())
        if text.strip() in ("", "#!/bin/sh"):
            f.unlink()
        else:
            f.write_text(text)


# ---------------------------------------------------------------- sharing

class project_lock:
    """Serializes mirror updates: the auto-push hook runs in the background."""

    def __init__(self, name: str):
        self.path = AGENT_HOME / f".push-{name}.lock"

    def __enter__(self):
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        self.fh.close()


def push_mirror(src: Path, name: str) -> None:
    mirror = mirror_of(name)
    if not mirror.is_dir():
        raise Fail(f"mirror for {name} is missing. Run: agent doctor")
    with project_lock(name):
        git(src, "push", "--quiet", "--force", "--prune", "--no-verify", mirror,
            "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")


def cmd_share(a) -> None:
    path = Path(a.path).expanduser().resolve()
    p = git(path, "rev-parse", "--show-toplevel", check=False)
    if p.returncode:
        raise Fail(f"not a git repository: {path}")
    src = Path(p.stdout.strip()).resolve()
    if src == AGENT_HOME or AGENT_HOME in src.parents:
        raise Fail(f"refusing to share a folder inside {AGENT_HOME}")
    name = a.name or re.sub(r"[^A-Za-z0-9_-]+", "-", src.name).strip("-")
    if not NAME_RE.fullmatch(name):
        raise Fail(f"invalid project name {name!r}: use letters, digits, - and _ (pass --name)")
    reg = load_registry()
    if name in reg:
        raise Fail(f"project {name!r} already shared")
    for other, info in reg.items():
        if Path(info["source"]) == src:
            raise Fail(f"this repo is already shared as {other!r}")
    if mirror_of(name).exists() or work_of(name).exists():
        raise Fail(f"leftover data for {name!r} in {AGENT_HOME}; remove it or pick another --name")
    branches = out(src, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    if not branches:
        raise Fail("the repo has no commits yet. Make a first commit, then share")
    current = git(src, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    default = current if current in branches else next(
        (b for b in ("main", "master") if b in branches), branches[0])
    if git(src, "remote", "get-url", "agent", check=False).returncode == 0:
        raise Fail("this repo already has a remote named 'agent'. Remove or rename it first")
    hooks_ok = not a.no_hooks and hooks_dir(src) is not None

    heading(f"Share {name}")
    kv([["Your repo", (str(src), "path")],
                     ["Branches", f"{len(branches)} (default {default}); committed history only"],
                     ["Agent clone", (f"/work/{name} in the container", "path")],
                     ["Stored in", (str(AGENT_HOME), "path")]])
    heading("Changes to your repo")
    bullet("+", "remote 'agent' (fetch only; pushing to it is disabled)")
    if hooks_ok:
        bullet("+", "hooks post-commit, post-merge, post-rewrite: auto-send your commits")
    elif a.no_hooks:
        bullet("-", "no hooks (--no-hooks): run `agent push` after you commit", "warn")
    else:
        bullet("-", "no hooks (core.hooksPath is set): run `agent push` after you commit", "warn")
    if not a.yes:
        _say(OUT, "", ("\n", ""), "Dry run. Re-run with --yes to share.", "warn")
        return

    for d in dirs().values():
        d.mkdir(parents=True, exist_ok=True)
    mirror, work = mirror_of(name), work_of(name)
    try:
        run(["git", "init", "--quiet", "--bare", mirror])
        git(mirror, "symbolic-ref", "HEAD", f"refs/heads/{default}")
        push_mirror(src, name)
        run(["git", "clone", "--quiet", "--no-hardlinks", "--origin", "desktop", mirror, work])
        git(work, "remote", "set-url", "desktop", f"/mirrors/{name}.git")
        git(work, "config", "remote.desktop.pushurl", NO_PUSH)
        git(work, "config", "--add", "remote.desktop.fetch", "+refs/wip/*:refs/remotes/desktop-wip/*")
        git(src, "remote", "add", "agent", work)
        git(src, "config", "remote.agent.fetch", "+refs/heads/agent/*:refs/remotes/agent/*")
        git(src, "config", "remote.agent.pushurl", NO_PUSH)
        git(src, "config", "remote.agent.tagopt", "--no-tags")
        (AGENT_HOME / "handoffs" / name).mkdir(parents=True, exist_ok=True)
        hooks = install_hooks(src, name) if hooks_ok else []
    except BaseException:
        git(src, "remote", "remove", "agent", check=False)
        shutil.rmtree(mirror, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        raise
    reg[name] = {"source": str(src), "hooks": bool(hooks)}
    save_registry(reg)
    ok(f"shared {name}. It appears in claude.ai/code as '{name}' within ~15 s (if the container is up).")
    note(f"First time only: `agent attach`, open window rc-{name}, and accept the folder trust prompt.")


# ---------------------------------------------------------------- merge records
# `agent merge` takes the agent's work as files (a squash), like HermesSync's
# sync: the agent's commits never enter your history. Once you commit, the
# agent commit you took is recorded in the mirror as refs/merged/<task>, so
# `pull` shows the task as merged and the agent continues from your latest work.

def git_path(src: Path, name: str) -> Path:
    p = Path(out(src, "rev-parse", "--git-path", name))
    return p if p.is_absolute() else src / p


def pending_file(name: str, task: str) -> Path:
    return AGENT_HOME / "pending" / name / f"{task}.json"


def merged_marker(name: str, task: str) -> str:
    p = git(mirror_of(name), "rev-parse", "-q", "--verify", f"refs/merged/{task}", check=False)
    return p.stdout.strip()


def is_ancestor(src: Path, a: str, b: str) -> bool:
    return git(src, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def settle(src: Path, name: str) -> None:
    """Record staged merges you've since committed; forget ones you abandoned."""
    folder = AGENT_HOME / "pending" / name
    if not folder.is_dir():
        return
    with project_lock(name):
        head = out(src, "rev-parse", "HEAD")
        in_progress = git_path(src, "SQUASH_MSG").exists() or git_path(src, "MERGE_HEAD").exists()
        for f in folder.glob("*.json"):
            info = json.loads(f.read_text())
            if in_progress:
                continue
            if head != info["head"]:
                git(src, "push", "--quiet", "--force", "--no-verify", mirror_of(name),
                    f"{info['sha']}:refs/merged/{info['task']}")
            f.unlink()  # recorded, or you undid the staged changes (git reset --merge)


def task_state(src: Path, name: str, task: str) -> str:
    ref = f"refs/remotes/agent/{task}"
    if pending_file(name, task).exists():
        return "staged; commit to finish"
    if is_ancestor(src, ref, "HEAD"):
        return "merged"
    marker, sha = merged_marker(name, task), out(src, "rev-parse", ref)
    if marker and (marker == sha or is_ancestor(src, sha, marker)):
        return "merged"
    if marker and is_ancestor(src, marker, sha):
        return f"{out(src, 'rev-list', '--count', f'{marker}..{sha}')} new since merge"
    return f"{out(src, 'rev-list', '--count', f'HEAD..{sha}')} new"


TRAILER = re.compile(r"(?im)^(co-authored-by|claude-session|signed-off-by|generated with)\b.*\n?")


def squash_message(src: Path, task: str, base: str, sha: str) -> str:
    span = [f"{base}..{sha}"] if base else ["-1", sha]
    subjects = [s for s in out(src, "log", "--reverse", "--no-merges", "--format=%s", *span).splitlines()
                if s and not s.startswith("wip: autosave")]
    if len(subjects) == 1:
        text = subjects[0] + "\n"
    else:
        title = task.replace("-", " ").replace("_", " ").strip().capitalize() or task
        text = title + "\n\n" + "".join(f"- {s}\n" for s in subjects)
    return TRAILER.sub("", sanitize(text))


# Never sent in a WIP snapshot, even when .gitignore doesn't cover them.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*")
SECRET_EXCLUDES = tuple(f":(exclude,glob)**/{p}" for p in SECRET_FILES)


def wip_snapshot(src: Path, branch: str) -> tuple[str, int]:
    """Commit everything in your working folder (edits, staged or not, and new files
    that .gitignore doesn't exclude) into a throwaway commit on top of HEAD, using a
    temporary index so your own staging area is untouched. Returns (sha, new files)."""
    with tempfile.TemporaryDirectory(prefix="agent-wip-") as tmp:
        index = Path(tmp) / "index"
        real = git_path(src, "index")
        if real.exists():
            shutil.copy2(real, index)
        env = {**clean_env(), "GIT_INDEX_FILE": str(index)}

        def g(*args) -> str:
            p = subprocess.run(["git", "-C", str(src), *args], env=env, text=True, capture_output=True)
            if p.returncode:
                raise Fail(f"WIP snapshot failed: git {' '.join(args[:2])}\n{(p.stderr or p.stdout).strip()}")
            return p.stdout.strip()

        g("add", "--all", "--", ".", *SECRET_EXCLUDES)
        tree = g("write-tree")
        if tree == out(src, "rev-parse", "HEAD^{tree}"):
            return "", 0
        added = len([line for line in g("diff", "--cached", "--name-only", "--diff-filter=A", "HEAD").splitlines() if line])
        sha = g("commit-tree", tree, "-p", "HEAD", "-m", f"WIP snapshot of {branch} (not committed by the user)")
        return sha, added


def cmd_push(a) -> None:
    name, src = resolve_project(a.project)
    push_mirror(src, name)
    settle(src, name)
    if not a.quiet:
        heading(f"Push to the agent · {name}")
    if a.wip:
        branch = out(src, "branch", "--show-current") or "detached"
        sha, added = wip_snapshot(src, branch)
        if sha:
            git(src, "push", "--quiet", "--force", "--no-verify", mirror_of(name), f"{sha}:refs/wip/{branch}")
            if not a.quiet:
                extra = f", including {added} new file{'s' if added != 1 else ''}" if added else ""
                ok(f"sent your uncommitted work{extra} as desktop-wip/{branch}")
                note("Your files and staging area are unchanged. Tell the agent to use desktop-wip/" + branch)
        elif not a.quiet:
            note("no uncommitted changes to send")
    if not a.quiet:
        ok(f"agent now sees your branches in {name}")


def fetch_agent(src: Path) -> None:
    git(src, "fetch", "--prune", "--quiet", "agent")


def agent_tasks(src: Path) -> list[str]:
    refs = out(src, "for-each-ref", "--format=%(refname)", "refs/remotes/agent/").splitlines()
    return [r.removeprefix("refs/remotes/agent/") for r in refs if r]


def require_task(src: Path, task: str) -> str:
    ref = f"refs/remotes/agent/{task}"
    if git(src, "rev-parse", "--verify", "-q", ref, check=False).returncode:
        have = ", ".join(agent_tasks(src)) or "none"
        raise Fail(f"no agent branch {task!r}. Available: {have}")
    return ref


def overwrite_with(name: str, src: Path, task: str) -> None:
    """Make your files exactly match agent/<task>, whatever either history looks like.
    Your previous uncommitted work is saved first, so this is never a dead end."""
    settle(src, name)
    fetch_agent(src)
    ref = require_task(src, task)
    sha = out(src, "rev-parse", ref)
    heading(f"Overwrite with agent/{task}")
    _, patch = close_review(src, name, task)
    if patch:
        note(f"Your review-copy edits are saved in {patch}")

    branch = out(src, "branch", "--show-current") or "detached"
    backup, _ = wip_snapshot(src, branch)          # works mid-merge and includes new files
    if backup:
        backup_ref = f"refs/agent-backup/{time.strftime('%Y%m%d-%H%M%S')}"
        git(src, "update-ref", backup_ref, backup)

    for f in (AGENT_HOME / "pending" / name).glob("*.json") if (AGENT_HOME / "pending" / name).is_dir() else ():
        f.unlink()                                  # any half-finished `agent merge` is replaced by this
    git(src, "reset", "--quiet")                    # leaves files alone; clears merge state and staging
    git(src, "read-tree", "--reset", "-u", sha)     # files and staging area = the agent's version
    git(src, "clean", "-fdq", *[x for p in SECRET_FILES for x in ("-e", p)])  # drop your other new files

    if out(src, "rev-parse", "HEAD^{tree}") == out(src, "rev-parse", f"{sha}^{{tree}}"):
        ok(f"your branch already matches agent/{task}; nothing to change")
    else:
        mb = git(src, "merge-base", "HEAD", sha, check=False)
        message = squash_message(src, task, mb.stdout.strip() if mb.returncode == 0 else "", sha)
        git_path(src, "SQUASH_MSG").write_text(message)
        pf = pending_file(name, task)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(json.dumps({"task": task, "sha": sha, "head": out(src, "rev-parse", "HEAD")}) + "\n")
        ok(f"your files now match agent/{task} exactly; the differences are staged, not committed")
        note("Check them in VS Code and commit. The message is pre-filled:")
        panel(message.strip(), "Commit message")
    if backup:
        note("Your previous uncommitted work is saved. To get it back:")
        hint("Restore", f"git restore --source={backup_ref} --worktree -- .")
    hint("To undo", "git reset --hard" + (f"   (then the Restore command above)" if backup else ""))


def cmd_pull(a) -> None:
    name, src = resolve_project(a.project)
    if a.overwrite:
        if not a.task:
            raise Fail("say which task to overwrite with: agent pull <task> --overwrite")
        overwrite_with(name, src, normalize_task(a.task))
        return
    settle(src, name)
    fetch_agent(src)
    tasks = agent_tasks(src)
    if not tasks:
        ok("no agent branches yet")
        return
    rows = []
    for task in tasks:
        ref = f"refs/remotes/agent/{task}"
        subject = sanitize(out(src, "log", "-1", "--format=%s", ref))[:60]
        ready = "written" if handoff_of(name, task).exists() else "-"
        state = task_state(src, name, task)
        style = "muted" if state == "merged" else "magenta" if state.startswith("staged") else "warn"
        rows.append([f"agent/{task}", (state, style), ("✓ written" if ready == "written" else "-",
                                                       "ok" if ready == "written" else "muted"), subject])
    heading(f"Agent branches in {name}")
    table(["Branch", "Commits", "Handoff", "Last commit"], rows, ["name", "", "", "muted"])
    hint("Review", "agent review <task>")
    hint("Take the changes", "agent merge <task>")


# ---------------------------------------------------------------- review copies
# A review copy is a detached worktree of YOUR repo: HEAD = the base, files = the
# agent's version, so VS Code shows the whole task as staged changes. If you edit
# it, those edits are saved as a patch before the copy is replaced or removed and
# carried into the next review copy or into the merge. Nothing you type is lost.

def reviewed_file(name: str, task: str) -> Path:
    return AGENT_HOME / "review" / name / f".{task}.reviewed"


def save_review_edits(name: str, task: str) -> Path | None:
    wt, meta = review_of(name, task), reviewed_file(name, task)
    if not wt.is_dir() or not meta.exists():
        return None
    reviewed = meta.read_text().strip()
    git(wt, "add", "-A")  # this worktree's own index only
    if git(wt, "diff", "--cached", "--quiet", reviewed, check=False).returncode == 0:
        return None
    patch = git(wt, "diff", "--cached", "--binary", reviewed).stdout
    f = AGENT_HOME / "review" / name / f"{task}-your-edits-{time.strftime('%Y%m%d-%H%M%S')}.patch"
    f.write_text(patch)
    return f


def close_review(src: Path, name: str, task: str) -> tuple[bool, Path | None]:
    """Remove the review copy. Returns (existed, patch of your edits or None)."""
    wt = review_of(name, task)
    if not wt.exists():
        return False, None
    patch = save_review_edits(name, task)
    git(src, "worktree", "remove", "--force", wt, check=False)
    shutil.rmtree(wt, ignore_errors=True)
    reviewed_file(name, task).unlink(missing_ok=True)
    git(src, "worktree", "prune")
    return True, patch


def apply_patch(repo: Path, patch: Path, three_way: bool) -> bool:
    args = ["apply", "--whitespace=nowarn"] + (["--3way"] if three_way else []) + [str(patch)]
    return git(repo, *args, check=False).returncode == 0


def cmd_review(a) -> None:
    name, src = resolve_project(a.project)
    task = normalize_task(a.task)
    if a.close:
        existed, patch = close_review(src, name, task)
        if patch:
            warn(f"you had edits in the review copy; saved them to {patch}")
            note(f"Apply them in your repo any time: git apply --3way {patch}")
        ok("review closed" if existed else "no open review")
        return
    settle(src, name)
    fetch_agent(src)
    ref = require_task(src, task)
    marker = merged_marker(name, task)
    if a.base == "HEAD" and marker and is_ancestor(src, marker, ref) and marker != out(src, "rev-parse", ref):
        base, label = marker, "what's new since you last merged it"
    else:
        mb = git(src, "merge-base", a.base, ref, check=False)
        if mb.returncode:  # unrelated histories: compare with your branch as it is
            base = out(src, "rev-parse", a.base)
            label = f"no shared history with your branch; showing how it differs from {a.base}"
        else:
            base, label = mb.stdout.strip(), f"against {a.base}"
    heading(f"Review agent/{task}  ({label} @ {base[:9]})")
    hand = handoff_of(name, task)
    if hand.exists():
        panel(sanitize(hand.read_text(errors="replace")).strip(), "Handoff")
    else:
        note("(no handoff written yet)")
    log = sanitize(out(src, "log", "--oneline", "--no-decorate", f"{base}..{ref}"))
    rows = [line.split(" ", 1) + [""] * (1 - line.count(" ")) for line in log.splitlines()]
    if rows:
        table(["Commit", "Message"], rows, ["yellow", ""])
    else:
        note("(no commits)")
    stat = out(src, "diff", "--shortstat", base, ref)
    if stat and OUT is not None:
        line = Text("  " + stat, style="muted")
        line.highlight_regex(r"\d+ insertions?\(\+\)", "ok")
        line.highlight_regex(r"\d+ deletions?\(-\)", "bad")
        emit(line)
    elif stat:
        note(stat)
    _, patch = close_review(src, name, task)
    wt = review_of(name, task)
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(src, "worktree", "add", "--quiet", "--detach", wt, ref)
    git(wt, "reset", "--quiet", "--soft", base)
    reviewed_file(name, task).write_text(out(src, "rev-parse", ref) + "\n")
    ok(f"review copy: {wt}")
    if patch:
        if apply_patch(wt, patch, three_way=False):
            ok("carried your edits from the previous review copy into this one (unstaged)")
            patch.unlink()
        else:
            warn(f"couldn't carry your previous edits over cleanly; they're saved in {patch}")
    if not a.no_open and shutil.which("code"):
        subprocess.Popen(["code", "--new-window", str(wt)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        note("opened in VS Code: Source Control lists every change the agent made")
    note("Small fixes: edit here or after `agent merge`; your edits come along either way.")
    hint("Take the changes", f"agent merge {task}")
    hint("Close", f"agent review {task} --close")


def cmd_merge(a) -> None:
    """Take the agent's changes as files in your repo, authored by you."""
    name, src = resolve_project(a.project)
    task = normalize_task(a.task)
    settle(src, name)
    if git_path(src, "SQUASH_MSG").exists() or git_path(src, "MERGE_HEAD").exists():
        raise Fail("a merge is already in progress in your repo. Commit it, or undo it with: git reset --merge")
    if out(src, "status", "--porcelain", "--untracked-files=no"):
        raise Fail("your repo has uncommitted changes. Commit or stash them first")
    fetch_agent(src)
    ref = require_task(src, task)
    sha = out(src, "rev-parse", ref)
    marker = merged_marker(name, task)
    if is_ancestor(src, ref, "HEAD") or (marker and (marker == sha or is_ancestor(src, sha, marker))):
        ok(f"agent/{task} is already merged; nothing new to take")
        return
    _, patch = close_review(src, name, task)
    heading(f"Merge agent/{task}")

    if a.merge:  # opt-in: keep the agent's own commits in your history
        p = git(src, "merge", "--no-ff", "-m", f"Merge agent/{task}", ref, check=False, capture=False)
        if p.returncode:
            warn("merge stopped on conflicts. In VS Code: Source Control → Merge Changes → resolve, then `git commit`.")
            note("To undo: git merge --abort")
            sys.exit(1)
        ok(f"merged agent/{task}, keeping the agent's commits. Push when ready: git push")
        return

    head = out(src, "rev-parse", "HEAD")
    if marker and is_ancestor(src, marker, sha):
        # The agent added commits after you last took this task: take only the new part.
        base = marker
        diff = AGENT_HOME / "pending" / f".{name}-{task}.diff"
        diff.parent.mkdir(parents=True, exist_ok=True)
        diff.write_text(out(src, "diff", "--binary", marker, sha) + "\n")
        conflicts = git(src, "apply", "--3way", "--whitespace=nowarn", diff, check=False).returncode != 0
        diff.unlink()
    else:
        mb = git(src, "merge-base", "HEAD", sha, check=False)
        if mb.returncode:
            raise Fail(f"agent/{task} doesn't share any history with your branch (the agent rewrote it), "
                       f"so it can't be merged. To make your files match it exactly: agent pull {task} --overwrite")
        base = mb.stdout.strip()
        conflicts = git(src, "merge", "--squash", ref, check=False).returncode != 0
    message = squash_message(src, task, base, sha)
    git_path(src, "SQUASH_MSG").write_text(message)
    pf = pending_file(name, task)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"task": task, "sha": sha, "head": head}) + "\n")

    if conflicts:
        warn("some changes conflict with yours. In VS Code: Source Control → Merge Changes → resolve and stage, then commit.")
        note("To undo: git reset --merge")
        if patch:
            note(f"Your review-copy edits are saved; after resolving, apply them: git apply --3way {patch}")
        sys.exit(1)
    if patch:
        if apply_patch(src, patch, three_way=True):
            ok("applied the edits you made in the review copy")
            patch.unlink()
        else:
            warn(f"couldn't apply your review-copy edits cleanly; they're saved in {patch}")
    if a.commit:
        git(src, "commit", "--quiet", "-F", git_path(src, "SQUASH_MSG"))
        settle(src, name)
        ok(f"committed as you: {message.splitlines()[0]}")
        note("Push when ready: git push")
        return
    ok(f"agent/{task}'s changes are staged in your repo, not committed")
    note("Check them in VS Code, fix anything you like, then commit. The message is pre-filled:")
    panel(message.strip(), "Commit message")
    hint("To undo", "git reset --merge")


def cmd_list(_a) -> None:
    reg = load_registry()
    if not reg:
        note("No shared repos yet.")
        hint("Share one", "agent share PATH --yes")
        return
    rows = []
    for name, info in sorted(reg.items()):
        src = Path(info["source"])
        flags = []
        if not src.is_dir():
            flags.append("repo missing")
        if not work_of(name).is_dir():
            flags.append("agent clone missing")
        if not info.get("hooks"):
            flags.append("manual push")
        rows.append([name, str(src), (", ".join(flags) or "✓ ok", "warn" if flags else "ok")])
    heading(f"Shared repos · {len(rows)}")
    table(["Project", "Your repo", "Status"], rows, ["name", "path", ""])


def cmd_unshare(a) -> None:
    name, src = resolve_project(a.project)
    pending = []
    if src.is_dir() and git(src, "remote", "get-url", "agent", check=False).returncode == 0:
        git(src, "fetch", "--prune", "--quiet", "agent", check=False)
        pending = [t for t in agent_tasks(src) if task_state(src, name, t) != "merged"]
    heading(f"Unshare {name}")
    say("Deletes the mirror, the agent's clone and handoffs; removes the remote and hooks from your repo.")
    if pending:
        warn(f"unmerged agent branches will be lost: {', '.join(pending)}")
    if not a.yes:
        if ask(f"Type {name} to confirm") != name:
            say("Cancelled.", "muted")
            return
    if src.is_dir():
        for task in agent_tasks(src):
            pending_file(name, task).unlink(missing_ok=True)
            _, patch = close_review(src, name, task)
            if patch:
                kept = Path.home() / patch.name
                shutil.move(str(patch), kept)
                warn(f"you had edits in the {task} review copy; saved them to {kept}")
        remove_hooks(src)
        git(src, "remote", "remove", "agent", check=False)
    for p in (mirror_of(name), work_of(name), AGENT_HOME / "handoffs" / name, AGENT_HOME / "review" / name,
              AGENT_HOME / "pending" / name):
        shutil.rmtree(p, ignore_errors=True)
    reg = load_registry()
    reg.pop(name, None)
    save_registry(reg)
    ok(f"unshared {name}")


# ---------------------------------------------------------------- container

def compose(*args, check=True):
    f = AGENT_HOME / "compose.yaml"
    if not f.exists():
        raise Fail("not set up on this machine yet. Run: agent setup")
    return run(["docker", "compose", "--project-directory", AGENT_HOME, "-f", f, *args],
               check=check, capture=False)


def container_running() -> bool:
    p = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], check=False)
    return p.returncode == 0 and p.stdout.strip() == "true"


def exec_in(*args) -> None:
    if not container_running():
        raise Fail("the container is not running. Start it: agent up")
    os.execvp("docker", ["docker", "exec", "-it", "-w", "/work", CONTAINER, *args])


def write_env() -> None:
    d = dirs()
    for p in d.values():
        if "'" in str(p) or "\n" in str(p):
            raise Fail(f"unsupported character in path: {p}")
    lines = [f"AGENT_UID={os.getuid()}", f"AGENT_GID={os.getgid()}", f"AGENT_IMAGE={IMAGE}"]
    for key in ("home", "work", "mirrors", "handoffs", "vault", "tools"):
        lines.append(f"AGENT_{key.upper()}_DIR='{d[key]}'")
    env = AGENT_HOME / ".env"
    env.write_text("# Generated by agent setup. Machine-specific; do not commit.\n" + "\n".join(lines) + "\n")
    env.chmod(0o600)


def install_claude_config() -> None:
    src, dst = REPO / "claude", dirs()["home"] / ".claude"
    dst.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in ("CLAUDE.md", "settings.json"):
        target, new = dst / f, (src / f).read_bytes()
        if target.exists() and target.read_bytes() != new:
            shutil.copy2(target, target.with_name(f"{f}.bak-{stamp}"))
            warn(f"updated {target.name} (previous saved as {f}.bak-{stamp})")
        target.write_bytes(new)
    skills = dst / "skills"
    skills.mkdir(exist_ok=True)
    for skill in sorted((src / "skills").iterdir()):
        if skill.is_dir():
            shutil.rmtree(skills / skill.name, ignore_errors=True)
            shutil.copytree(skill, skills / skill.name)


def install_gitconfig() -> None:
    home = dirs()["home"]
    shutil.copy2(REPO / "container" / "gitconfig", home / ".gitconfig.agent")
    main = home / ".gitconfig"
    text = main.read_text() if main.exists() else ""
    if ".gitconfig.agent" not in text:
        main.write_text(text + "[include]\n\tpath = .gitconfig.agent\n")


def restore_git_safety() -> None:
    """Undo changes the agent made to its git safety settings (what `agent checkup` flags)."""
    base = AGENT_HOME.resolve()
    targets = [base / "home" / ".gitconfig"] + [base / "work" / n / ".git" / "config" for n in load_registry()]
    for cfg in targets:
        if not cfg.exists() or not no_links(base, cfg) or not cfg.is_file():
            continue  # a link could point at one of your own files: leave it for checkup to report
        for key in git_config_file(cfg):
            if RISKY_GIT_KEYS.match(key) and key not in ("include.path", "remote.desktop.pushurl"):
                run(["git", "config", "--file", cfg, "--unset-all", key], check=False)
                warn(f"removed {key} from {cfg.relative_to(base)}")
        if cfg.name == "config":
            name = cfg.parent.parent.name
            for key, value in (("remote.desktop.url", f"/mirrors/{name}.git"), ("remote.desktop.pushurl", NO_PUSH)):
                if git_config_file(cfg).get(key) != [value]:
                    run(["git", "config", "--file", cfg, "--replace-all", key, value])
                    warn(f"restored {key} for {name}")


def init_vault() -> None:
    vault = dirs()["vault"]
    if (vault / ".git").exists() or any(vault.iterdir()):
        note(f"keeping existing vault at {vault}")
        return
    shutil.copytree(REPO / "vault-template", vault, dirs_exist_ok=True)
    run(["git", "init", "--quiet", "--initial-branch=main", vault])
    git(vault, "add", "-A")
    git(vault, "-c", "user.name=agent setup", "-c", "user.email=agent@localhost",
        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "vault: create")
    ok(f"created Obsidian vault at {vault} (open this folder in Obsidian)")


def init_tools() -> None:
    """The agent's permanent tools folder. env.sh and README.md are ours; the rest is the agent's."""
    tools, template = dirs()["tools"], REPO / "tools-template"
    for sub in ("bin", "opt", "jdks", "sysroot", "env.d", "python", "npm", "downloads", "caches"):
        (tools / sub).mkdir(parents=True, exist_ok=True)
    for f in ("env.sh", "README.md"):
        target, new = tools / f, (template / f).read_bytes()
        if target.is_symlink():
            target.unlink()
        elif f == "env.sh" and target.exists() and target.read_bytes() != new:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(target, target.with_name(f"env.sh.bak-{stamp}"))
            warn(f"restored tools/env.sh (the changed one is saved as env.sh.bak-{stamp})")
        target.write_bytes(new)
    manifest = tools / "manifest.json"
    if not manifest.exists() and not manifest.is_symlink():
        manifest.write_text('{\n  "tools": []\n}\n')


def machine_file() -> Path:
    return AGENT_HOME / "machine.json"


def build_image(version: str | None) -> None:
    info = json.loads(machine_file().read_text()) if machine_file().exists() else {}
    version = version or info.get("claude_code") or "latest"
    check_docker_space()
    heading(f"Building {IMAGE} with Claude Code {version}")
    run(["docker", "build", "--build-arg", f"CLAUDE_CODE_VERSION={version}", "-t", IMAGE,
         REPO / "container"], capture=False)
    freed = remove_old_images()  # each rebuild leaves the previous image untagged
    if freed:
        ok(f"removed the previous image ({human(freed)} freed)")
    p = run(["docker", "run", "--rm", "--entrypoint", "claude", IMAGE, "--version"], check=False)
    m = re.search(r"\d+\.\d+\.\d+", p.stdout or "")
    info.update({"claude_code": m.group(0) if m else version, "built": time.strftime("%Y-%m-%d %H:%M")})
    machine_file().write_text(json.dumps(info, indent=2) + "\n")
    ok(f"image built; Claude Code {info['claude_code']} pinned for rebuilds on this machine")


def preflight() -> None:
    if not shutil.which("git"):
        raise Fail("git is not installed")
    if not shutil.which("docker"):
        raise Fail("Docker is not installed. See https://docs.docker.com/engine/install/")
    if run(["docker", "compose", "version"], check=False).returncode:
        raise Fail("the Docker Compose plugin is missing")
    if run(["docker", "info"], check=False).returncode:
        raise Fail("cannot reach the Docker daemon. Start Docker, or add yourself to the docker group and log in again")


def choose_home(a) -> None:
    """Settle where the agent keeps its data: --home, the saved choice, or ask once."""
    global AGENT_HOME
    if a.home:
        new = Path(a.home).expanduser().resolve()
        if os.environ.get("AGENT_HOME") and Path(os.environ["AGENT_HOME"]).expanduser().resolve() != new:
            raise Fail("AGENT_HOME is set in your environment and differs from --home")
        if (AGENT_HOME / ".env").exists() and AGENT_HOME.resolve() != new:
            raise Fail(f"already set up in {AGENT_HOME}. To move it: agent storage move {new}")
    elif os.environ.get("AGENT_HOME") or load_config().get("home") or AGENT_HOME.exists() or not interactive():
        return
    else:
        new = pick_home()
    prepare_home(new)
    AGENT_HOME = new
    save_config(home=str(new))
    ok(f"the agent keeps its data in {new}")
    d = disk(new)
    if d and d[0] < LOW_FREE:
        warn(f"only {human(d[0])} free there")


def cmd_setup(a) -> None:
    if not a.no_build or not a.no_start:
        preflight()
    choose_home(a)
    heading("Setup")
    for d in dirs().values():
        d.mkdir(parents=True, exist_ok=True)
    write_env()
    shutil.copy2(REPO / "container" / "compose.yaml", AGENT_HOME / "compose.yaml")
    install_claude_config()
    install_gitconfig()
    restore_git_safety()
    init_vault()
    init_tools()
    if not a.no_build:
        build_image(a.claude_version)
    if not a.no_start:
        compose("up", "-d")
    ok(f"set up in {AGENT_HOME}")
    root = docker_root()
    d = disk(root) if root else None
    if d and d[0] < LOW_FREE:
        warn(f"Docker's disk ({root}) has only {human(d[0])} free.")
        hint("Move Docker's data to a bigger disk", f"agent storage docker-move {suggest_docker_root()}")
    if not any(f.exists() for f in completion_targets().values()):
        hint("Tab completion", "agent completion install")
    hint("Next (once per machine)", "agent login")
    hint("Then", "agent share PATH --yes")


def cmd_doctor(_a) -> None:
    counts = {"bad": 0, "warn": 0, "good": 0}

    def check(label: str, good: bool, hint_: str = "", level: str = "bad") -> None:
        state = "good" if good else level
        counts[state] += 1
        tail = "" if good or not hint_ else f"  → {hint_}"
        mark = {"good": "✓", "warn": "!", "bad": "✗"}[state]
        if OUT is None:
            print(f"  {mark} {label}{tail}")
        else:
            emit(hang(Text(f"  {mark} ", {"good": "ok", "warn": "warn", "bad": "bad"}[state]),
                      Text.assemble(label, (tail, "muted"))))

    heading("agent doctor")
    subheading("Tools")
    docker = shutil.which("docker") is not None
    check("git installed", shutil.which("git") is not None)
    check("docker installed", docker)
    launcher = shutil.which("agent")
    me = str(Path(__file__).resolve())
    mine = launcher is not None and (Path(launcher).resolve() == Path(me)
                                     or me in Path(launcher).read_text(errors="replace"))
    check("`agent` on PATH", mine, "run ./install.sh, or add ~/.local/bin to PATH")
    check("pretty output (Rich)", OUT is not None or os.environ.get("AGENT_PLAIN") == "1",
          "re-run ./install.sh (Ubuntu may need: sudo apt install python3-venv)")
    check("shell completion installed", any(f.exists() for f in completion_targets().values()),
          "agent completion install", level="warn")

    subheading("Container")
    reachable = docker and run(["docker", "info"], check=False).returncode == 0
    if docker:
        check("docker daemon reachable", reachable)
        check("image built", run(["docker", "image", "inspect", IMAGE], check=False).returncode == 0, "agent setup")
        check("container running", container_running(), "agent up")
    check("set up", (AGENT_HOME / "compose.yaml").exists(), "agent setup")
    if docker and container_running():
        st = run(["docker", "exec", CONTAINER, "claude", "auth", "status"], check=False).stdout
        check("logged in to Claude", bool(re.search(r'"loggedIn":\s*true', st)), "agent login")
    check("vault exists", (dirs()["vault"] / ".git").is_dir(), "agent setup")
    check("tools folder exists", (dirs()["tools"] / "env.sh").is_file(), "agent setup")

    subheading("Storage")
    d = disk(AGENT_HOME)
    if d:
        check(f"agent data: {human(d[0])} free at {AGENT_HOME}", d[0] >= LOW_FREE,
              "agent storage move /bigger/disk/Agent", level="warn")
    root = docker_root() if reachable else None
    dd = disk(root) if root else None
    if dd:
        check(f"docker data: {human(dd[0])} free at {root}", dd[0] >= LOW_FREE,
              "agent storage clean, or move it: agent storage docker-move PATH", level="warn")
    if reachable:
        old = old_images()
        check(f"{len(old)} old agent image(s) left by rebuilds" if old else "no old agent images", not old,
              f"agent storage clean (frees up to {human(sum(i['size'] for i in old))})", level="warn")

    if load_registry():
        subheading("Projects")
    for name, info in sorted(load_registry().items()):
        src = Path(info["source"])
        url = git(src, "remote", "get-url", "agent", check=False).stdout.strip() if src.is_dir() else ""
        check(f"{name}: repo, mirror, clone, remote",
              src.is_dir() and mirror_of(name).is_dir() and work_of(name).is_dir() and url == str(work_of(name)),
              f"agent unshare --project {name} --yes, then share again")

    close_box()
    if counts["bad"]:
        error(f"{counts['bad']} problem(s), {counts['warn']} warning(s)")
    elif counts["warn"]:
        warn(f"healthy, with {counts['warn']} warning(s)")
    else:
        ok(f"all {counts['good']} checks passed")
    if not counts["bad"]:
        hint("To check what the agent has been doing", "agent checkup")
    sys.exit(1 if counts["bad"] else 0)


# ---------------------------------------------------------------- checkup
# "Has the agent behaved?" A read-only report. Everything it looks at was
# written by the agent, so it's read as plain data: links are never followed,
# nothing from the agent's folders is run, and printed text is sanitized. The
# only git commands it runs in agent-controlled folders read objects, with every
# config option that could run a program switched off.

# Patterns are matched where a command starts (not inside words or file text), and
# the bodies of heredocs fed to non-shell programs (python3 - <<EOF …) are skipped:
# they're file contents or code, and mentioning "docker" there isn't running it.
CMD = (r"(?:^|\n|[;&|(`{]|\$\(|\b(?:then|do|else)\s)\s*(?:[A-Za-z_]\w*=\S*\s+)*"
       r"(?:(?:command|exec|nohup|time|nice|env)\s+|timeout\s+(?:-\S+\s+)*\S+\s+)*")
RISKY_COMMANDS = [
    (re.compile(CMD + r"(sudo|doas|su)(\s|$)"), "tried to become root"),
    (re.compile(r"\b(curl|wget)\b[^|;&\n]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"), "piped a download into a shell"),
    (re.compile(r"base64\s+(-d|--decode)\b[^|;&\n]*\|\s*(ba|z|da)?sh\b"), "ran decoded text as a script"),
    (re.compile(CMD + r"chmod\s+(-\w+\s+)*(\S*[ug]\+\S*s|0?[2-7][0-7]{3})\b"), "made a setuid/setgid file"),
    (re.compile(CMD + r"git\s+(-\S+\s+(\S+\s+)?)*push\b"), "tried to push"),
    (re.compile(r"(?i)" + CMD + r"git\s+(-\S+\s+(\S+\s+)?)*config\b[^;&|\n]*\b(pushurl|insteadof|hookspath|credential|"
                r"fsmonitor|sshcommand|include)"), "changed a git safety setting"),
    (re.compile(CMD + r"git\s+(-\S+\s+(\S+\s+)?)*((commit|rebase|merge|am|cherry-pick)\b[^;&|\n]*--no-verify|"
                      r"commit\b[^;&|\n]*\s-[A-Za-z]*n[A-Za-z]*\b)"), "skipped git hooks"),
    (re.compile(CMD + r"rm\s+(-\w+\s+)*-\w*[rR]\w*\s+(-\w+\s+)*[\"']?(/|~|\$HOME|/work|/vault|/home/agent|/mirrors|"
                r"/handoffs|/tools)/?[\"']?(\s|$|[;&|])"), "deleted a whole top-level folder"),
    (re.compile(r"(>>?|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\brm\b|\bln\b|\bchmod\b)[^;&|\n]*"
                r"(/home/agent/|~/|\$HOME/)\.(claude|gitconfig)"), "changed its own settings"),
    (re.compile(r"host\.docker\.internal|\b172\.17\.0\.1\b|\b192\.168\.\d+\.\d+\b|\b10\.\d+\.\d+\.\d+\b"),
     "reached for your PC or home network"),
    (re.compile(CMD + r"(ssh|scp|sftp|nc|ncat|socat|telnet)\s"), "opened a connection to another machine"),
    (re.compile(CMD + r"docker(\s|$)|docker\.sock"), "tried to use Docker"),
    (re.compile(CMD + r"(nohup|setsid|disown|crontab)\b|\bnohup\s"), "left a process running in the background"),
]
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SHELLISH = re.compile(r"(^|[\s;&|(])((ba|z|da|k)?sh|source|eval|sudo|\.)(\s|$)")


def without_data_heredocs(cmd: str) -> str:
    """The command with heredoc bodies removed, unless the heredoc feeds a shell."""
    keep, skip_until = [], None
    for line in cmd.splitlines():
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        keep.append(line)
        m = HEREDOC.search(line)
        if m and not SHELLISH.search(line[:m.start()]):
            skip_until = m.group(2)
    return "\n".join(keep)


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PROTECTED_EDITS = re.compile(r"^(/home/agent/\.(claude|gitconfig)|/tools/env\.sh$|/opt/agent/|.*/\.git/(hooks/|config$))")
SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}"), "GitHub token"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "API key"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]
# Files that programs on your PC act on by themselves once they're in your repo.
AUTO_RUN_FILES = re.compile(r"(^|/)(\.claude/|\.mcp\.json$|\.vscode/|\.devcontainer/|\.envrc$|\.github/workflows/|"
                            r"\.husky/|\.pre-commit-config\.yaml$|\.gitattributes$|\.gitmodules$|CLAUDE\.md$)")
RISKY_GIT_KEYS = re.compile(r"^(core\.(hookspath|fsmonitor|sshcommand|pager|askpass)|credential\.|url\..*insteadof$|"
                            r"include\.|includeif\.|remote\..*\.(pushurl|proxy|uploadpack|receivepack)$)")
EXPECTED_PROCS = re.compile(r"docker-init|tini|agent-supervisor|tmux|claude|^sleep \d+$|^-?bash$|^/bin/bash$")
MAX_ITEMS = 8


def checkup_state_file() -> Path:
    return DATA_DIR / "agent" / "checkup.json"  # on your side: the agent can't move the date


def no_links(base: Path, path: Path) -> bool:
    """True if nothing between base and path (inclusive) is a symlink."""
    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    cur = base
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return False
    return True


def agent_read(base: Path, path: Path, limit: int = 20 * 1024 * 1024) -> str | None:
    """A file the agent wrote, as text; None if it's missing, a link, not a file, or huge."""
    if not no_links(base, path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        return f.read().decode("utf-8", "replace")


def agent_files(base: Path, top: Path, suffix: str = ""):
    """Regular files under top, never following links."""
    if not top.is_dir() or not no_links(base, top):
        return
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for n in filenames:
            p = Path(dirpath) / n
            if n.endswith(suffix) and not p.is_symlink() and p.is_file():
                yield p


def agent_git(gitdir: Path, *args) -> subprocess.CompletedProcess:
    """Read-only git in the agent's clone, with nothing its config could make git run."""
    env = {**clean_env(), "GIT_DIR": str(gitdir), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"}
    safe = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
            "-c", "core.sshCommand=false", "-c", "protocol.allow=never"]
    return subprocess.run(["git", "--no-pager", *safe, *args], env=env, text=True, capture_output=True)


def git_config_file(path: Path) -> dict[str, list[str]]:
    """Every key in one git config file (includes aren't followed)."""
    p = subprocess.run(["git", "config", "--file", str(path), "--list"], env=clean_env(), text=True,
                       capture_output=True)
    found: dict[str, list[str]] = {}
    for line in p.stdout.splitlines():
        key, _, value = line.partition("=")
        found.setdefault(key.lower(), []).append(value)
    return found


def mask(secret: str) -> str:
    return secret[:6] + "…" if len(secret) > 8 else "…"


def find_secrets(text: str) -> list[tuple[int, str]]:
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for rx, kind in SECRET_PATTERNS:
            m = rx.search(line)
            if m:
                hits.append((i, f"{kind} {mask(m.group(0))}"))
    return hits


def parse_time(stamp: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))  # UTC
    except (ValueError, TypeError):
        return 0.0


def scan_transcripts(base: Path, since: float) -> dict:
    """Bash commands and file edits from the saved chats, newest last."""
    found = {"chats": set(), "commands": 0, "flagged": []}
    for f in agent_files(base, base / "home" / ".claude" / "projects", ".jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        text = agent_read(base, f, limit=200 * 1024 * 1024) or ""
        for line in text.splitlines():
            if '"tool_use"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            when = parse_time(str(entry.get("timestamp", "")))
            content = (entry.get("message") or {}).get("content")
            if when < since or not isinstance(content, list):
                continue
            project = Path(str(entry.get("cwd") or "")).name or "?"
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name, inp = item.get("name"), item.get("input") or {}
                found["chats"].add(str(entry.get("sessionId") or f))
                if name == "Bash":
                    found["commands"] += 1
                    cmd = str(inp.get("command", ""))
                    shown = without_data_heredocs(cmd)
                    for rx, label in RISKY_COMMANDS:
                        if rx.search(shown):
                            found["flagged"].append((when, project, label, cmd))
                            break
                elif name in EDIT_TOOLS:
                    path = str(inp.get("file_path") or inp.get("notebook_path") or "")
                    if PROTECTED_EDITS.match(path):
                        found["flagged"].append((when, project, "edited its own settings", f"{name} {path}"))
    found["flagged"].sort(key=lambda x: x[0])
    return found


def load_tools_module():
    import importlib.machinery
    import importlib.util
    path = REPO / "container" / "bin" / "tools"
    loader = importlib.machinery.SourceFileLoader("agent_tools", str(path))
    spec = importlib.util.spec_from_loader("agent_tools", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


PORTS_SCRIPT = r"""
cat /proc/net/tcp /proc/net/tcp6 2>/dev/null
echo ---
for p in /proc/[0-9]*; do
  for fd in "$p"/fd/*; do
    l=$(readlink "$fd" 2>/dev/null) || continue
    case "$l" in socket:*) echo "${p#/proc/} $l" ;; esac
  done
done
echo ---
for p in /proc/[0-9]*; do printf '%s ' "${p#/proc/}"; tr '\0' ' ' < "$p/cmdline" 2>/dev/null; echo; done
"""


def decode_addr(hexaddr: str) -> str:
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return ".".join(str(b) for b in reversed(raw))
    words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]  # /proc stores IPv6 as four little-endian words
    import ipaddress
    return str(ipaddress.IPv6Address(b"".join(words)))


def listening_ports() -> list[tuple[int, str, str]]:
    """(port, address, process) for every listening TCP socket in the container."""
    p = run(["docker", "exec", CONTAINER, "/bin/sh", "-c", PORTS_SCRIPT], check=False)
    parts = (p.stdout or "").split("\n---\n")
    if len(parts) < 3:
        return []
    sockets, pids, cmds = parts[0], parts[1], parts[2]
    owner = {}
    for line in pids.splitlines():
        pid, _, link = line.partition(" ")
        m = re.fullmatch(r"socket:\[(\d+)\]", link.strip())
        if m:
            owner.setdefault(m.group(1), pid)
    cmdline = {}
    for line in cmds.splitlines():
        pid, _, cmd = line.partition(" ")
        cmdline[pid] = cmd.strip()
    found = set()
    for line in sockets.splitlines():
        f = line.split()
        if len(f) > 9 and f[3] == "0A" and ":" in f[1]:
            hexaddr, _, hexport = f[1].partition(":")
            try:
                addr = decode_addr(hexaddr)
            except ValueError:
                addr = "?"
            found.add((int(hexport, 16), addr, sanitize(cmdline.get(owner.get(f[9], ""), ""))))
    return sorted(found)


def cmd_checkup(a) -> None:
    base = AGENT_HOME.resolve()
    if not (base / ".env").exists():
        raise Fail("not set up on this machine yet. Run: agent setup")
    state_file = checkup_state_file()
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get(str(base))
    started = time.time()
    since = 0.0 if a.all else (last or started - 30 * 86400)
    since_text = "all history" if a.all else (
        "since your last checkup, " + time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last
        else "the last 30 days (first checkup)")
    counts = {"bad": 0, "warn": 0, "good": 0}
    fixes: list[tuple[str, str]] = []

    def check(label: str, items: list[str], level: str = "bad", fix: tuple[str, str] | None = None,
              good_label: str = "") -> None:
        state_ = level if items else "good"
        counts[state_] += 1
        mark = {"good": "✓", "warn": "!", "bad": "✗"}[state_]
        text = good_label if not items and good_label else label
        if OUT is None:
            print(f"  {mark} {text}")
        else:
            emit(hang(Text(f"  {mark} ", {"good": "ok", "warn": "warn", "bad": "bad"}[state_]), Text(text)))
        for item in items[:MAX_ITEMS]:
            note(f"    {sanitize(item)[:160]}")
        if len(items) > MAX_ITEMS:
            note(f"    … and {len(items) - MAX_ITEMS} more")
        if items and fix and fix not in fixes:
            fixes.append(fix)

    heading("agent checkup")
    note(f"Looking at {since_text}. Nothing here is run or changed.")
    home = base / "home"

    # -- its own config
    subheading("Its own settings")
    expected = {home / ".claude" / "settings.json": REPO / "claude" / "settings.json",
                home / ".claude" / "CLAUDE.md": REPO / "claude" / "CLAUDE.md",
                home / ".gitconfig.agent": REPO / "container" / "gitconfig"}
    for f in (REPO / "claude" / "skills").rglob("*"):
        if f.is_file():
            expected[home / ".claude" / "skills" / f.relative_to(REPO / "claude" / "skills")] = f
    changed = []
    for target, source in expected.items():
        text = agent_read(base, target)
        if text is None:
            changed.append(f"{target.relative_to(home)}: missing, or replaced by a link")
        elif text.encode() != source.read_bytes():
            changed.append(f"{target.relative_to(home)}: changed")
    check("its settings, instructions or skills were changed", changed,
          fix=("Restore the agent's settings (keeps a backup)", "agent setup --no-build"),
          good_label="settings, instructions and skills are as installed")
    added = []
    skills_dir = home / ".claude" / "skills"
    ours = {p.name for p in (REPO / "claude" / "skills").iterdir() if p.is_dir()}
    if skills_dir.is_dir() and no_links(base, skills_dir):
        added += [f"skill: {p.name}" for p in sorted(skills_dir.iterdir()) if p.name not in ours | {"synced"}]
    for sub in ("agents", "commands", "hooks"):
        d = home / ".claude" / sub
        if d.is_dir() and any(d.iterdir()):
            added.append(f"{sub}/: " + ", ".join(sorted(p.name for p in d.iterdir())[:5]))
    if (home / ".claude" / "settings.local.json").exists() or (home / ".claude" / "settings.local.json").is_symlink():
        added.append("settings.local.json (it can override permissions and hooks)")
    plugins = agent_read(base, home / ".claude" / "plugins" / "installed_plugins.json")
    try:
        names = list((json.loads(plugins) or {}).get("plugins") or {}) if plugins else []
    except (ValueError, AttributeError):
        names = []
    if names:
        added.append("plugins: " + ", ".join(map(str, names[:5])))
    claude_json = agent_read(base, home / ".claude.json")
    try:
        cj = json.loads(claude_json) if claude_json else {}
        servers = set((cj.get("mcpServers") or {}).keys())
        for proj in (cj.get("projects") or {}).values():
            if isinstance(proj, dict):
                servers |= set((proj.get("mcpServers") or {}).keys())
    except (ValueError, AttributeError):
        servers = set()
    if servers:
        added.append("MCP servers: " + ", ".join(sorted(map(str, servers))))
    check("added skills, plugins, hooks or MCP servers (fine if you added them)", added, level="warn",
          good_label="no skills, plugins, hooks or MCP servers added")
    synced = skills_dir / "synced"  # Claude Code copies your claude.ai account's skills here
    if synced.is_dir() and no_links(base, synced):
        names = sorted({f.parent.name for f in agent_files(base, synced, "SKILL.md")})
        if names:
            note(f"  {len(names)} skills synced from your claude.ai account: {', '.join(names)}. Each adds its "
                 "description to every chat; turn off ones you don't use in claude.ai's skill settings.")

    # -- git safety
    subheading("Git safety")
    problems = []
    gitconfig = home / ".gitconfig"
    if agent_read(base, gitconfig) is None:
        problems.append("~/.gitconfig: missing, or replaced by a link")
    else:
        keys = git_config_file(gitconfig)
        if ".gitconfig.agent" not in keys.get("include.path", []):
            problems.append("~/.gitconfig no longer loads the agent's git settings (.gitconfig.agent)")
        problems += [f"~/.gitconfig sets {k}" for k in keys if RISKY_GIT_KEYS.match(k) and k != "include.path"]
    reg = load_registry()
    branch_issues = []
    for name in sorted(reg):
        gitdir = base / "work" / name / ".git"
        cfg = gitdir / "config"
        if not gitdir.is_dir() or agent_read(base, cfg) is None:
            problems.append(f"{name}: the agent's clone is missing, or its .git was replaced")
            continue
        keys = git_config_file(cfg)
        if keys.get("remote.desktop.url") != [f"/mirrors/{name}.git"]:
            problems.append(f"{name}: remote 'desktop' doesn't point at the read-only mirror any more")
        if keys.get("remote.desktop.pushurl") != [NO_PUSH]:
            problems.append(f"{name}: pushing to 'desktop' is no longer disabled")
        problems += [f"{name}: sets {k}" for k in keys if RISKY_GIT_KEYS.match(k) and k != "remote.desktop.pushurl"]
        others = sorted({k.split(".")[1] for k in keys if k.startswith("remote.") and k.count(".") >= 2} - {"desktop"})
        if others:
            branch_issues.append(f"{name}: extra remotes: {', '.join(others)}")
        refs = agent_git(gitdir, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/").stdout
        for line in refs.splitlines():
            branch, _, sha = line.partition(" ")
            if branch.startswith("agent/") or not sha:
                continue
            # Commits on your branch names that you never had: made by the agent with the hook skipped.
            # (A branch left behind after you rewrote your history isn't flagged: those commits are yours.)
            mine = agent_git(gitdir, "log", "-n", "200", "--format=%ce", sha, "--not", "--remotes=desktop",
                             "--remotes=desktop-merged", "--remotes=desktop-wip").stdout.split()
            n = sum(1 for e in mine if e == AGENT_EMAIL)
            if n:
                branch_issues.append(f"{name}: {n} agent commit(s) on '{branch}', outside agent/* "
                                     "(the commit hook was skipped)")
    check("its git safety settings were changed", problems,
          fix=("Restore the agent's git settings", "agent setup --no-build"),
          good_label="pushing is disabled and the commit hook is in place")
    check("unexpected branches or remotes", branch_issues, level="warn",
          good_label="commits only on agent/* branches")

    # -- what it ran
    subheading("Commands it ran")
    tr = scan_transcripts(base, since)
    flagged = [f"{time.strftime('%m-%d %H:%M', time.localtime(w))}  {p}  {label}:  {' '.join(c.split())[:110]}"
               for w, p, label, c in tr["flagged"]]
    check(f"{len(flagged)} worth a look ({tr['commands']} commands in {len(tr['chats'])} chat(s))", flagged,
          level="warn", good_label=f"nothing risky in {tr['commands']} command(s) across {len(tr['chats'])} chat(s)")

    # -- tools
    subheading("Tools folder")
    tools_dir = base / "tools"
    if not tools_dir.is_dir() or not no_links(base, tools_dir):
        check("no tools folder (or it was replaced by a link)", ["run: agent setup --no-build"], level="warn")
    else:
        r = load_tools_module().scan(tools_dir)
        check("links pointing outside /tools, or setuid files", r["links"] + [f"setuid: {p}" for p in r["setuid"]]
              + ([r["problem"]] if r["problem"] else []),
              fix=("Ask the agent", "run `tools check` and fix what it reports"),
              good_label="no links out of the folder, no setuid files")
        check("files not in the manifest", r["unrecorded"], level="warn",
              fix=("Ask the agent", "run `tools check` and fix what it reports"),
              good_label=f"{len(r['entries'])} tool(s), all recorded ({human(r['size'])})")
        check("installed without a checksum", r["unverified"], level="warn", good_label="every download was verified")
        env_sh = agent_read(base, tools_dir / "env.sh")
        check("tools/env.sh was changed (settings belong in env.d/)",
              [] if env_sh is not None and env_sh.encode() == (REPO / "tools-template" / "env.sh").read_bytes()
              else ["tools/env.sh"], level="warn", fix=("Restore it (keeps a backup)", "agent setup --no-build"),
              good_label="tools/env.sh is as installed")
        local = home / ".local" / "bin"
        outside = sorted(p.name for p in local.iterdir()) if local.is_dir() and no_links(base, local) else []
        if outside:
            note(f"  Also in its home folder, outside /tools: {', '.join(outside[:8])}. Fine, but /tools is the place now.")

    # -- secrets
    subheading("Secrets")
    secrets = []
    for top in (base / "vault", base / "handoffs"):
        for f in agent_files(base, top):
            if f.suffix.lower() in (".md", ".txt", ".json", ".yaml", ".yml", ""):
                secrets += [f"{f.relative_to(base)}:{n}  {what}" for n, what in find_secrets(agent_read(base, f) or "")]
    risky_files = []
    for name, info in sorted(reg.items()):
        src = Path(info["source"])
        if not src.is_dir():
            continue
        git(src, "fetch", "--prune", "--quiet", "agent", check=False)
        for task in agent_tasks(src):
            ref = f"refs/remotes/agent/{task}"
            try:
                if task_state(src, name, task) == "merged":
                    continue
            except Fail:
                continue
            mb = git(src, "merge-base", "HEAD", ref, check=False).stdout.strip()
            if not mb:
                continue
            names = git(src, "diff", "--name-only", "--no-renames", mb, ref, check=False).stdout.splitlines()
            risky_files += [f"{name} agent/{task}: {n}" for n in names if AUTO_RUN_FILES.search(n)]
            diff = git(src, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", mb, ref, check=False).stdout
            current = "?"
            for line in diff[:20 * 1024 * 1024].splitlines():
                if line.startswith("+++ "):
                    current = line[6:] if line.startswith("+++ b/") else line[4:]
                elif line.startswith("+"):
                    secrets += [f"{name} agent/{task}: {current}  {what}" for _, what in find_secrets(line[1:])]
    check("possible secrets in the vault, handoffs or agent branches", secrets,
          good_label="no keys or tokens found in the vault, handoffs or unmerged branches")
    check("files your own tools act on by themselves (look closely before merging)", risky_files, level="warn",
          good_label="no editor, CI or Claude config files in unmerged branches")

    # -- container
    subheading("Container right now")
    if not (shutil.which("docker") and container_running()):
        note("  The container isn't running, so processes and ports were skipped.")
    else:
        top = run(["docker", "top", CONTAINER, "-eo", "pid,etime,args"], check=False).stdout.splitlines()[1:]
        odd = []
        for line in top:
            parts = line.split(None, 2)
            if len(parts) == 3 and not EXPECTED_PROCS.search(parts[2]):
                odd.append(f"{parts[2][:100]}  (running for {parts[1]})")
        check("other processes running (normal while a chat is working; otherwise left behind)", odd, level="warn",
              good_label="only the usual processes")
        exposed, local = [], []
        for port, addr, cmd in listening_ports():
            if addr == "127.0.0.11" and not cmd:
                continue  # Docker's built-in DNS server for the container's network
            if addr.startswith("127.") or addr == "::1":
                if not re.search(r"\bclaude\b", cmd):  # Claude Code's own isn't worth mentioning
                    local.append(f"port {port}: {cmd[:80] or 'unknown process'}")
            else:
                exposed.append(f"port {port} on {addr}: {cmd[:80] or 'unknown process'}")
        check("listening on network ports reachable from your PC", exposed, level="warn",
              good_label="no network ports open to your PC")
        if exposed:
            note("  Not your network: the container publishes no ports. Fine if a chat is running a dev server now.")
        if local:
            note("  Also listening inside the container only (harmless): " + "; ".join(local[:5]))

    # -- activity
    subheading("Activity")
    note(f"  {since_text[0].upper()}{since_text[1:]}; agent commits as of your last `agent pull` or checkup.")
    rows = [["Chats", f"{len(tr['chats'])}, {tr['commands']} commands"]]
    notes_changed = [f for f in agent_files(base, base / "vault", ".md") if f.stat().st_mtime >= since
                     and f.name not in ("Index.md", "README.md")]
    rows.append(["Vault notes changed", ", ".join(sorted(f.stem for f in notes_changed)[:6]) or "none"])
    handoffs = [f for f in agent_files(base, base / "handoffs", ".md") if f.stat().st_mtime >= since]
    rows.append(["Handoffs written", ", ".join(sorted(f"{f.parent.name}/{f.stem}" for f in handoffs)[:6]) or "none"])
    if tools_dir.is_dir() and no_links(base, tools_dir):
        day = time.strftime("%Y-%m-%d", time.localtime(since)) if since else ""
        entries = load_tools_module().read_manifest(tools_dir)[0]
        new = [f"{e.get('name')} {e.get('version', '')}" for e in entries if str(e.get("updated", "")) >= day]
        rows.append(["Tools installed", ", ".join(map(str, new[:6])) or "none"])
    commits = []
    for name, info in sorted(reg.items()):
        src = Path(info["source"])
        if src.is_dir():
            n = git(src, "rev-list", "--count", "--since=" + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)), "--remotes=agent", "--not", "HEAD", check=False).stdout.strip()
            if n and n != "0":
                commits.append(f"{name} {n}")
    rows.append(["Agent commits", ", ".join(commits) or "none"])
    kv([[k, sanitize(v)] for k, v in rows])

    close_box()
    if not a.no_save:
        state[str(base)] = started
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2) + "\n")
    for label, cmd in fixes:
        hint(label, cmd)
    close_box()
    if counts["bad"]:
        error(f"{counts['bad']} problem(s), {counts['warn']} thing(s) to look at")
    elif counts["warn"]:
        warn(f"no problems; {counts['warn']} thing(s) to look at")
    else:
        ok(f"all {counts['good']} checks passed")
    sys.exit(1 if counts["bad"] else 0)


# ---------------------------------------------------------------- storage
# Where the agent's data lives (AGENT_HOME, chosen at setup) and what Docker
# keeps on its own disk: the image, old untagged images left by rebuilds, and
# the build cache.

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "bcachefs", "reiserfs", "apfs", "hfs"}
SKIP_MOUNTS = ("/boot", "/efi", "/snap", "/var/lib/docker", "/var/snap", "/run", "/proc", "/sys", "/dev", "/tmp")


def existing(path: Path) -> Path:
    path = Path(path).expanduser()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def disk(path) -> tuple[int, int] | None:
    """(free, total) bytes on the filesystem holding path (or its nearest existing parent)."""
    try:
        u = shutil.disk_usage(existing(Path(path)))
        return u.free, u.total
    except OSError:
        return None


def same_disk(a, b) -> bool:
    try:
        return os.stat(existing(Path(a))).st_dev == os.stat(existing(Path(b))).st_dev
    except OSError:
        return False


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    p = run(["du", "-sk", path], check=False)
    try:
        return int(p.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def real_mounts() -> list[Path]:
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in REAL_FS:
            continue
        mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
        if any(mnt == s or mnt.startswith(s + "/") for s in SKIP_MOUNTS) or not os.path.isdir(mnt):
            continue
        found.append(Path(mnt))
    return found


def can_create(path: Path) -> bool:
    return os.access(existing(path), os.W_OK)


def candidate_locations() -> list[dict]:
    """~/Agent plus an Agent folder on each other real disk, one per filesystem."""
    user = os.environ.get("USER") or Path.home().name
    paths = [Path.home() / "Agent"]
    for mnt in real_mounts():
        if mnt == Path("/"):
            continue
        paths.append(mnt / "Agent" if os.access(mnt, os.W_OK) or mnt == Path("/") else mnt / user / "Agent")
    seen, out_ = set(), []
    for path in paths:
        try:
            key = os.statvfs(existing(path)).f_fsid or os.stat(existing(path)).st_dev
        except OSError:
            continue
        d = disk(path)
        if key in seen or not d:
            continue
        seen.add(key)
        out_.append({"path": path, "free": d[0], "total": d[1], "ready": can_create(path)})
    return out_


def pick_home() -> Path:
    """Ask where the agent keeps its data, suggesting the disk with the most free space."""
    cands = candidate_locations()
    if not cands:
        return Path.home() / "Agent"
    best = max(range(len(cands)), key=lambda i: cands[i]["free"])
    root = docker_root()
    heading("Where should the agent keep its data?")
    note("Repos the agent works on, mirrors, review copies, its Claude login and the vault go here.")
    rows = []
    for i, c in enumerate(cands, 1):
        tags = []
        if c["path"].is_relative_to(Path.home()):
            tags.append("your home")
        if root and same_disk(c["path"], root):
            tags.append("Docker's disk")
        if not c["ready"]:
            tags.append("needs sudo to create")
        free_style = "bad" if c["free"] < LOW_FREE else "ok" if i - 1 == best else ""
        rows.append([(f"{i}" + (" ★" if i - 1 == best else ""), "head"), (str(c["path"]), "path"),
                     (human(c["free"]), free_style), human(c["total"]), ", ".join(tags)])
    table(["#", "Location", "Free", "Disk size", ""], rows)
    answer = ask(f"Choose 1-{len(cands)}, or type a path", str(best + 1))
    if answer.isdigit() and 1 <= int(answer) <= len(cands):
        return cands[int(answer) - 1]["path"]
    return Path(answer).expanduser().resolve()


def prepare_home(path: Path) -> None:
    """Create the data folder, with sudo (after asking) if its parent isn't yours."""
    if path.is_dir() and os.access(path, os.W_OK):
        return
    if can_create(path):
        path.mkdir(parents=True, exist_ok=True)
        return
    cmd = ["sudo", "install", "-d", "-o", str(os.getuid()), "-g", str(os.getgid()), str(path)]
    if interactive() and confirm(f"{existing(path)} isn't writable by you. Create {path} with sudo?", True):
        run(cmd, capture=False)
        return
    raise Fail(f"can't create {path}. Run: {' '.join(cmd)}\nthen run setup again")


def docker_info(fmt: str) -> str:
    if not shutil.which("docker"):
        return ""
    p = run(["docker", "info", "-f", fmt], check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def docker_root() -> Path | None:
    root = docker_info("{{.DockerRootDir}}")
    return Path(root) if root else None


def old_images() -> list[dict]:
    """Untagged agent images left behind by rebuilds (recognised by their entrypoint)."""
    p = run(["docker", "images", "-q", "--no-trunc", "-f", "dangling=true"], check=False)
    ids = sorted(set(p.stdout.split())) if p.returncode == 0 else []
    if not ids:
        return []
    p = run(["docker", "image", "inspect", "-f", "{{.Id}} {{.Size}} {{json .Config.Entrypoint}}", *ids], check=False)
    found = []
    for line in p.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and ENTRYPOINT in parts[2]:
            found.append({"id": parts[0], "size": int(parts[1])})
    return found


def remove_old_images() -> int:
    old = old_images()
    if old:
        run(["docker", "rmi", *[i["id"] for i in old]], check=False)
    return sum(i["size"] for i in old)


def build_cache_size() -> str:
    p = run(["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"], check=False)
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "Build Cache" and len(parts) == 3:
            return parts[1]
    return ""


def check_docker_space(needed: int = BUILD_NEEDS) -> None:
    """Before building: warn if Docker's disk is nearly full, and say what to do."""
    root = docker_root()
    d = disk(root) if root else None
    if not d or d[0] >= needed:
        return
    warn(f"Docker keeps images in {root}, which has only {human(d[0])} free (a build needs about {human(needed)}).")
    hint("Free space", "agent storage clean --build-cache")
    hint("Or move Docker's data to a bigger disk", f"agent storage docker-move {suggest_docker_root()}")
    if interactive() and not confirm("Build anyway?"):
        raise Fail("cancelled")


def suggest_docker_root() -> str:
    cands = candidate_locations()
    root = docker_root()
    others = [c for c in cands if not root or not same_disk(c["path"], root)]
    if not others:
        return "/data/docker"
    best = max(others, key=lambda c: c["free"])["path"]
    return str((best.parent if best.name == "Agent" else best) / "docker")


def cmd_storage(a) -> None:
    heading("Agent data")
    if AGENT_HOME.exists():
        with status("measuring …"):
            sizes = {n: dir_size(p) for n, p in dirs().items()}
        total = dir_size(AGENT_HOME)
    else:
        sizes, total = {}, 0
    d = disk(AGENT_HOME)
    rows = [["Location", (str(AGENT_HOME), "path")], ["Using", (human(total), "bold")]]
    if d:
        rows.append(["Disk free", (f"{human(d[0])} of {human(d[1])}", "bad" if d[0] < LOW_FREE else "ok")])
    parts = ", ".join(f"{n} {human(v)}" for n, v in sorted(sizes.items(), key=lambda kv: -kv[1]) if v)
    if parts:
        rows.append(["Breakdown", (parts, "muted")])
    kv(rows)

    heading("Docker")
    root = docker_root()
    if not root:
        note("Docker isn't reachable, so its disk use can't be measured.")
        return
    dd = disk(root)
    img = run(["docker", "image", "inspect", "-f", "{{.Size}}", IMAGE], check=False)
    old = old_images()
    cache = build_cache_size()
    ctr = run(["docker", "ps", "-a", "-s", "--filter", f"name=^/{CONTAINER}$", "--format", "{{.Size}}"],
              check=False).stdout.strip()
    rows = [["Location", (str(root), "path")]]
    if dd:
        rows.append(["Disk free", (f"{human(dd[0])} of {human(dd[1])}", "bad" if dd[0] < LOW_FREE else "ok")])
    rows.append(["Agent image", human(int(img.stdout.strip())) if img.returncode == 0 else ("not built", "muted")])
    rows.append(["Old agent images", (f"{len(old)}, up to {human(sum(i['size'] for i in old))}", "warn")
                 if old else ("none", "muted")])
    if cache:
        rows.append(["Build cache", (f"{cache} (all your Docker builds)", "")])
    if ctr:
        rows.append(["Container", (f"{ctr} (its own files outside the mounted folders)", "")])
    kv(rows)
    if old or cache not in ("", "0B"):
        hint("Free Docker space", "agent storage clean" + ("" if not cache or cache == "0B" else " --build-cache"))
    if dd and dd[0] < LOW_FREE:
        hint("Move Docker's data to a bigger disk", f"agent storage docker-move {suggest_docker_root()}")
    if d and d[0] < LOW_FREE:
        hint("Move the agent's data", "agent storage move /bigger/disk/Agent")


def cmd_storage_clean(a) -> None:
    root = docker_root()
    if not root:
        raise Fail("cannot reach Docker")
    before = disk(root)
    freed = remove_old_images()
    ok(f"removed old agent images ({human(freed)})" if freed else "no old agent images to remove")
    if a.build_cache:
        note("The build cache only speeds up rebuilds; Docker recreates it when needed.")
        if a.yes or (interactive() and confirm("Clear Docker's whole build cache (all projects, not just this one)?")):
            run(["docker", "builder", "prune", "-f"], capture=False, check=False)
        else:
            note("kept the build cache (pass --yes to clear it without asking)")
    after = disk(root)
    if before and after:
        ok(f"{human(max(after[0] - before[0], 0))} freed; {human(after[0])} now free at {root}")


def cmd_storage_move(a) -> None:
    """Move AGENT_HOME to another disk and point everything at the new place."""
    global AGENT_HOME
    old, new = AGENT_HOME.resolve(), Path(a.path).expanduser().resolve()
    if os.environ.get("AGENT_HOME"):
        raise Fail("AGENT_HOME is set in your environment; change it there instead")
    if not old.is_dir():
        raise Fail(f"nothing to move: {old} doesn't exist")
    if new == old or old in new.parents or new in old.parents:
        raise Fail("the new location must be outside the current one")
    if new.exists() and any(new.iterdir()):
        raise Fail(f"{new} exists and isn't empty")
    if "'" in str(new) or "\n" in str(new):
        raise Fail(f"unsupported character in path: {new}")
    size, d = dir_size(old), disk(new)
    heading("Move the agent's data")
    kv([["From", (str(old), "path")], ["To", (str(new), "path")], ["Size", human(size)],
                     ["Free there", human(d[0]) if d else "?"]])
    if d and d[0] < size * 1.1:
        raise Fail(f"not enough space at {new}")
    if not a.yes and not (interactive() and confirm("Stop the container and move it?", True)):
        say("Cancelled. Re-run with --yes to move.", "muted")
        return
    prepare_home(new)
    was_running = shutil.which("docker") is not None and container_running()
    if was_running:
        compose("down")
    with status(f"moving {human(size)} …"):
        if same_disk(old, new):
            new.rmdir()
            os.rename(old, new)
        else:
            run(["cp", "-a", f"{old}/.", str(new)])
            shutil.rmtree(old)
    AGENT_HOME = new
    if old == Path.home() / "Agent":
        old.symlink_to(new)  # Obsidian and old habits keep working
    save_config(home=str(new))
    write_env()
    for name, info in load_registry().items():
        src = Path(info["source"])
        if src.is_dir():
            git(src, "remote", "set-url", "agent", work_of(name), check=False)
            reviews = [p for p in (new / "review" / name).glob("*") if p.is_dir()]
            if reviews:
                git(src, "worktree", "repair", *reviews, check=False)
    ok(f"moved to {new}" + (f" (and {old} now links there)" if old.is_symlink() else ""))
    if was_running:
        compose("up", "-d")


def cmd_docker_move(a) -> None:
    """Print the commands to move Docker's own data (needs root, so we don't run them)."""
    target = Path(a.path).expanduser()
    root = docker_root()
    if not root:
        raise Fail("cannot reach Docker")
    os_name = docker_info("{{.OperatingSystem}}")
    heading("Move Docker's data")
    note(f"Docker keeps images, containers and build cache in {root}. These commands move all of it")
    note(f"to {target}. They need root, so run them yourself; nothing changes until you do.")
    if "Docker Desktop" in os_name:
        hint("Docker Desktop", "Settings → Resources → Advanced → Disk image location")
        return
    rootless = root.is_relative_to(Path.home())
    conf = Path.home() / ".config" / "docker" / "daemon.json" if rootless else Path("/etc/docker/daemon.json")
    try:
        current = json.loads(conf.read_text()) if conf.exists() else {}
    except (OSError, ValueError):
        current = None
    merged = json.dumps({**(current or {}), "data-root": str(target)})
    sudo, ctl = ("", "systemctl --user") if rootless else ("sudo ", "sudo systemctl")
    steps = [
        f"{ctl} stop docker docker.socket",
        f"{sudo}mkdir -p {shlex.quote(str(target))}",
        f"{sudo}rsync -aHAX --info=progress2 {shlex.quote(str(root))}/ {shlex.quote(str(target))}/",
        f"echo {shlex.quote(merged)} | {sudo}tee {conf}",
        f"{ctl} start docker",
        "docker info -f '{{.DockerRootDir}}'   # should print " + str(target),
        f"{sudo}rm -rf {shlex.quote(str(root))}   # only once everything works",
    ]
    if current is None:
        warn(f"couldn't read {conf}; add \"data-root\": \"{target}\" to it by hand instead of step 4")
    if OUT is None:
        print("\n".join(f"  {s}" for s in steps))
    else:
        from rich.syntax import Syntax
        emit(Panel(Syntax("\n".join(steps), "bash", theme="ansi_dark", background_color="default"),
                   border_style="muted", box=box.ROUNDED, expand=False))
    note("rsync must be installed. After this, `agent up` starts the container from the moved data.")


# ---------------------------------------------------------------- shell completion
# `agent __complete SHELL WORDS…` prints "value<TAB>description<TAB>group" lines
# from the argparse tree, plus live values: task names, projects, refs. The
# scripts below call it; `agent completion install` puts one where your shell looks.

def completion_targets() -> dict[str, Path]:
    return {"bash": DATA_DIR / "agent" / "completion.bash",
            "zsh": DATA_DIR / "agent" / "completion.zsh",
            "fish": Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "fish" / "completions" / "agent.fish"}


def _project_for(words: list[str]) -> tuple[str, Path] | None:
    """--project, else the shared repo you're in (or its agent clone or review copy), else the only one."""
    reg = load_registry()
    try:
        if "--project" in words[:-1]:
            return resolve_project(words[words.index("--project") + 1])
        return resolve_project(None)
    except (Fail, IndexError, OSError, ValueError):
        pass
    cwd = Path.cwd().resolve()
    for name, info in reg.items():
        for d in (work_of(name), AGENT_HOME / "review" / name):
            d = d.resolve()
            if cwd == d or d in cwd.parents:
                return name, Path(info["source"])
    if len(reg) == 1:
        name, info = next(iter(reg.items()))
        return name, Path(info["source"])
    return None


def task_names(name: str, src: Path) -> list[tuple[str, str]]:
    """(task, last commit subject) from the agent's own clone, so tasks show up before `agent pull`."""
    found: dict[str, str] = {}
    for repo, prefix in ((src, "refs/remotes/agent/"), (work_of(name), "refs/heads/agent/")):
        if not repo.is_dir():
            continue
        p = git(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname)\t%(subject)", prefix, check=False)
        for line in p.stdout.splitlines():
            ref, _, subject = line.partition("\t")
            found[ref.removeprefix(prefix)] = subject
    return list(found.items())


def complete_values(act: argparse.Action, words: list[str]) -> list[tuple[str, str, str]]:
    dest = act.dest
    if act.choices:
        return [(str(c), "", "value") for c in act.choices]
    if dest == "project":
        return [(n, i["source"], "project") for n, i in sorted(load_registry().items())]
    if dest in ("path", "target", "home"):
        return [(":dirs", "", "")]
    proj = _project_for(words)
    if not proj:
        return []
    if dest == "task":
        return [(t, subject, "task") for t, subject in task_names(*proj)]
    if dest == "base":
        p = git(proj[1], "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/tags", check=False)
        return [(r, "", "ref") for r in ["HEAD", *p.stdout.split()]]
    return []


def complete(words: list[str]) -> list[tuple[str, str, str]]:
    """Candidates for the last word, given the words before it."""
    parser, cur, positional, pending = build_parser(), words[-1], 0, None
    for w in words[:-1]:
        if pending is not None:
            pending = None
            continue
        if w.startswith("-"):
            act = parser._option_string_actions.get(w.split("=", 1)[0])
            if act is not None and act.nargs != 0 and "=" not in w:
                pending = act
            continue
        subs = [x for x in parser._actions if isinstance(x, argparse._SubParsersAction)]
        if subs and w in subs[0].choices:
            parser, positional = subs[0].choices[w], 0
        else:
            positional += 1
    if pending is not None:
        return complete_values(pending, words)
    if cur.startswith("-"):
        used = set(words[:-1])
        return [(opt, act.help or "", "option") for act in parser._actions if act.help != argparse.SUPPRESS
                for opt in act.option_strings if opt.startswith("--") and opt not in used]
    found = []
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            found += [(c.dest, c.help or "", "command") for c in act._choices_actions]
            return found
    positionals = [x for x in parser._actions if not x.option_strings and x.nargs != argparse.SUPPRESS]
    if positional < len(positionals):
        found += complete_values(positionals[positional], words)
    return found


COMPLETION_SCRIPTS = {
    "bash": r"""# agent shell completion for bash (generated by: agent completion bash)
_agent() {
  local cur=${COMP_WORDS[COMP_CWORD]} value desc group width=${COLUMNS:-80} longest=0
  local -a values descs
  COMPREPLY=()
  while IFS=$'\t' read -r value desc group; do
    case $value in
      :files) compopt -o filenames 2>/dev/null; mapfile -t -O "${#COMPREPLY[@]}" COMPREPLY < <(compgen -f -- "$cur") ;;
      :dirs)  compopt -o filenames 2>/dev/null; mapfile -t -O "${#COMPREPLY[@]}" COMPREPLY < <(compgen -d -- "$cur") ;;
      *) values+=("$value"); descs+=("$desc"); (( ${#value} > longest )) && longest=${#value} ;;
    esac
  done < <(agent __complete bash "${COMP_WORDS[@]:1:COMP_CWORD}" 2>/dev/null)
  if (( ${#values[@]} == 1 )); then
    COMPREPLY+=("${values[0]}")
  elif (( ${#values[@]} > 1 )); then
    # Several matches: list each with its description, one per line (the way zsh and fish do).
    local i line
    for i in "${!values[@]}"; do
      line=$(printf '%-*s' "$longest" "${values[i]}")
      [[ -n ${descs[i]} ]] && line+="  -- ${descs[i]}"
      COMPREPLY+=("$(printf '%-*.*s' "$((width - 1))" "$((width - 1))" "$line")")
    done
  fi
}
complete -F _agent agent
# Highlight the part you've typed in the list of matches (AGENT_COMPLETION_COLORS=0 to skip).
if [[ $- == *i* && ${AGENT_COMPLETION_COLORS:-1} != 0 ]]; then
  bind 'set colored-completion-prefix on' 2>/dev/null
fi
""",
    "zsh": r"""# agent shell completion for zsh (generated by: agent completion zsh)
_agent() {
  local line value desc group rest
  local -a lines command task project ref option value
  lines=("${(@f)$(agent __complete zsh "${(@)words[2,CURRENT]}" 2>/dev/null)}")
  for line in $lines; do
    value=${line%%$'\t'*}; rest=${line#*$'\t'}; desc=${rest%%$'\t'*}; group=${rest#*$'\t'}
    case $value in
      :files) _files; continue ;;
      :dirs)  _files -/; continue ;;
      '') continue ;;
    esac
    [[ -n $group ]] || continue
    set -A $group "${(@P)group}" "${value//:/\\:}:$desc"
  done
  (( $#command )) && _describe -t commands 'command' command
  (( $#task ))    && _describe -t tasks 'agent task' task
  (( $#project )) && _describe -t projects 'shared repo' project
  (( $#ref ))     && _describe -t refs 'ref' ref
  (( $#value ))   && _describe -t values 'value' value
  (( $#option ))  && _describe -t options 'option' option
  return 0
}
zstyle ':completion:*:*:agent:*' group-name ''
zstyle ':completion:*:*:agent:*:descriptions' format '%F{cyan}%B── %d%b%f'
zstyle ':completion:*:*:agent:*:commands' list-colors '=(#b)([^ ]#)*=0=1;35'
zstyle ':completion:*:*:agent:*:tasks' list-colors '=(#b)([^ ]#)*=0=1;32'
zstyle ':completion:*:*:agent:*:projects' list-colors '=(#b)([^ ]#)*=0=1;32'
zstyle ':completion:*:*:agent:*:options' list-colors '=(#b)([^ ]#)*=0=33'
(( $+functions[compdef] )) || { autoload -Uz compinit && compinit -i; }
compdef _agent agent
""",
    "fish": r"""# agent shell completion for fish (generated by: agent completion fish)
function __agent_complete
    set -l words (commandline -opc)[2..-1] (commandline -ct)
    agent __complete fish $words 2>/dev/null | while read -l line
        set -l parts (string split \t -- $line)
        switch $parts[1]
            case :files
                __fish_complete_path (commandline -ct)
            case :dirs
                __fish_complete_directories (commandline -ct)
            case '*'
                printf '%s\t%s\n' $parts[1] $parts[2]
        end
    end
end
complete -c agent -f -a '(__agent_complete)'
""",
}
RC_LINE = "[ -r {f} ] && . {f}  # agent shell completion"


def cmd_complete(argv: list[str]) -> None:
    shell, words = (argv[0] if argv else "bash"), (argv[1:] or [""])
    try:
        found = complete(words)
    except Exception:  # never break the user's shell
        return
    cur = words[-1]
    for value, desc, group in found:
        if value.startswith(":") or shell == "zsh" or value.startswith(cur):
            print(f"{value}\t{sanitize(desc).replace(chr(9), ' ')}\t{group}")


def cmd_completion(a) -> None:
    if a.action != "install":
        sys.stdout.write(COMPLETION_SCRIPTS[a.action])
        return
    shell = a.shell or Path(os.environ.get("SHELL", "bash")).name
    if shell not in COMPLETION_SCRIPTS:
        raise Fail(f"no completion for {shell!r}; supported: bash, zsh, fish")
    f = completion_targets()[shell]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(COMPLETION_SCRIPTS[shell])
    heading("Tab completion")
    ok(f"{shell} completion installed: {f}")
    old = DATA_DIR / "bash-completion" / "completions" / "agent"  # earlier location; would load an old copy
    if shell == "bash" and old.exists() and "agent __complete" in old.read_text():
        old.unlink()
    if shell in ("bash", "zsh"):
        rc = Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc" if shell == "zsh" else Path.home() / ".bashrc"
        line = RC_LINE.format(f=shlex.quote(str(f)))
        text = rc.read_text() if rc.exists() else ""
        if line not in text:
            rc.write_text(text + ("" if text.endswith("\n") or not text else "\n") + line + "\n")
            note(f"added a line to {rc} that loads it")
    hint("Open a new terminal (or run: exec " + shell + "), then try", "agent review <Tab><Tab>")


# ---------------------------------------------------------------- update
# The install folder is a git clone. `agent update` fast-forwards it (or, for a
# shallow clone made with --depth 1, replaces it with the new tip and drops the
# old objects, so only the current version is kept), then re-runs install.sh.
# The image is rebuilt only when container/ changed or Claude Code is upgraded.

def latest_claude_code() -> str:
    """The newest Claude Code on npm, asked through the image's own npm (no npm needed here)."""
    if not shutil.which("docker"):
        return ""
    p = run(["docker", "run", "--rm", "--entrypoint", "npm", IMAGE, "view", "@anthropic-ai/claude-code", "version"],
            check=False)
    m = re.search(r"\d+\.\d+\.\d+", p.stdout or "")
    return m.group(0) if m else ""


def cmd_update(a) -> None:
    if not (REPO / ".git").exists():
        raise Fail(f"{REPO} isn't a git clone, so it can't update itself. Re-install with:\n"
                   f"git clone --depth 1 <repo url> {REPO} && {REPO}/install.sh")
    heading("Update agent")
    branch = git(REPO, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    upstream = git(REPO, "rev-parse", "--abbrev-ref", "@{u}", check=False).stdout.strip()
    if not upstream:
        if not branch:
            raise Fail(f"{REPO} is on a detached HEAD; check out a branch first")
        upstream = f"origin/{branch}"
    remote, _, rbranch = upstream.partition("/")
    shallow = out(REPO, "rev-parse", "--is-shallow-repository") == "true"
    with status(f"checking {remote} for updates …"):
        git(REPO, "fetch", "--quiet", *(["--depth", "1"] if shallow else []), remote, rbranch)
        latest = latest_claude_code() if a.claude or a.check else ""
    old, new = out(REPO, "rev-parse", "HEAD"), out(REPO, "rev-parse", "FETCH_HEAD")
    info = json.loads(machine_file().read_text()) if machine_file().exists() else {}
    pinned = info.get("claude_code", "")
    changed = out(REPO, "diff", "--name-only", old, new).splitlines() if old != new else []
    rebuild = any(f.startswith("container/") for f in changed)

    kv([["Install folder", (tilde(REPO) + ("  (shallow copy)" if shallow else ""), "path")],
        ["Installed", (f"{old[:9]}  {out(REPO, 'log', '-1', '--format=%s', old)}", "")],
        ["Available", (f"{new[:9]}  {out(REPO, 'log', '-1', '--format=%s', new)}", "ok") if old != new
         else ("up to date", "ok")],
        ["Claude Code", (pinned or "?") + (f"  →  {latest} available" if latest and latest != pinned else
                                         "  (newest)" if latest else "")]])
    if old != new and not shallow:
        log = sanitize(out(REPO, "log", "--oneline", "--no-decorate", f"{old}..{new}"))
        if log:
            table(["Commit", "What's new"], [line.split(" ", 1) + [""] * (1 - line.count(" "))
                                              for line in log.splitlines()[:15]], ["sha", ""])
    if changed:
        note(f"{len(changed)} file(s) change" + ("; the image will be rebuilt" if rebuild else
                                                 "; the image stays as it is"))
    if a.check:
        if old != new or (latest and latest != pinned):
            hint("Update", "agent update" + (" --claude" if latest and latest != pinned else ""))
        return
    if old == new and not a.claude and not a.reinstall:
        ok("already up to date")
        return
    if old != new:
        if out(REPO, "status", "--porcelain", "--untracked-files=no"):
            raise Fail(f"{REPO} has local changes. Commit or stash them, then update")
        if shallow:
            git(REPO, "reset", "--quiet", "--hard", new)
            # Keep only the new version: drop the old commit and its files.
            git(REPO, "reflog", "expire", "--expire=now", "--all", check=False)
            git(REPO, "gc", "--quiet", "--prune=now", check=False)
        elif git(REPO, "merge", "--quiet", "--ff-only", new, check=False).returncode:
            raise Fail(f"{REPO} has commits that aren't on {upstream}; update it by hand (git pull)")
        ok(f"updated to {new[:9]}")
    if a.no_install:
        hint("Finish later", f"{REPO}/install.sh")
        return
    args = [str(REPO / "install.sh")]
    if a.claude:
        args += ["--claude-version", "latest"]
    elif not rebuild:
        args.append("--no-build")  # nothing in the image changed
    if container_running() and (a.claude or rebuild):
        note("The container restarts with the new image; running sessions reconnect on their own.")
    close_box()
    os.execvp("bash", ["bash", *args])


# ---------------------------------------------------------------- help
# argparse's help and errors, drawn in the same boxes as everything else.

COMMAND_GROUPS = (
    ("Daily", ("pull", "review", "merge", "push")),
    ("Repos", ("share", "unshare", "list")),
    ("Container", ("up", "down", "restart", "logs", "attach", "shell", "login")),
    ("This machine", ("setup", "update", "doctor", "checkup", "storage", "completion")),
)


def _backticks(text: str) -> Text:
    """Plain text with `command` spans highlighted."""
    t = Text()
    for i, part in enumerate(re.split(r"`([^`]+)`", text)):
        t.append(part, "cmd" if i % 2 else "")
    return t


class AgentParser(argparse.ArgumentParser):
    def print_help(self, file=None):
        if OUT is None:
            return super().print_help(file)
        render_help(self)

    def error(self, message):
        m = re.search(r"invalid choice: '([^']*)'", message)
        if m:
            subs = [x for x in self._actions if isinstance(x, argparse._SubParsersAction)]
            names = [c.dest for c in subs[0]._choices_actions] if subs else []
            close = difflib.get_close_matches(m.group(1), names, n=2)
            message = f"unknown command '{m.group(1)}'" + (f". Did you mean: {' or '.join(close)}?" if close else "")
        if OUT is None:
            return super().error(message)
        error(message)
        hint("Help", f"{self.prog} --help")
        sys.exit(2)


def render_help(p: argparse.ArgumentParser) -> None:
    fmt = p._get_formatter()
    if p.prog == "agent":
        banner([("agent", "bold"), (p.description or "", "muted"), ("", "")])
    heading(p.prog)
    if p.description and p.prog != "agent":
        emit(Text(p.description))
    # rich-argparse colours the usage with raw escape codes: parse them, or Rich counts them as
    # characters (the box's right edge comes out short) and their resets wipe the border colour.
    usage = Text.from_ansi(re.sub(r"\n\s*", " ", p.format_usage().strip()), style="bold")
    prefix = re.match(r"(?i)usage:\s*", usage.plain)
    line = Table.grid(padding=(0, 2))
    line.add_column(style="muted", no_wrap=True)
    line.add_column(overflow="fold")
    line.add_row("Usage", usage[prefix.end():] if prefix else usage)
    emit(line)

    def rows(actions):
        g = Table.grid(padding=(0, 2))
        g.add_column(no_wrap=True)
        g.add_column(style="muted", overflow="fold")
        for act in actions:
            inv = fmt._format_action_invocation(act)
            flag, _, meta = inv.partition(" ") if act.option_strings and act.nargs != 0 else (inv, "", "")
            g.add_row(Text.assemble(("  " + flag, "name"), (" " + meta if meta else "", "sha")),
                      Text(fmt._expand_help(act) if act.help else ""))
        return g

    subs = [x for x in p._actions if isinstance(x, argparse._SubParsersAction)]
    if subs:
        choices = {c.dest: c for c in subs[0]._choices_actions}
        groups = COMMAND_GROUPS if p.prog == "agent" else (("Commands", tuple(choices)),)
        listed = set()
        for title, names in groups:
            acts = [choices[n] for n in names if n in choices]
            if acts:
                subheading(title)
                emit(rows(acts))
                listed.update(names)
        rest = [c for n, c in choices.items() if n not in listed]
        if rest:
            subheading("More")
            emit(rows(rest))
    pos = [x for x in p._actions if not x.option_strings and not isinstance(x, argparse._SubParsersAction)
           and x.help != argparse.SUPPRESS]
    if pos:
        subheading("Arguments")
        emit(rows(pos))
    opts = [x for x in p._actions if x.option_strings and x.help != argparse.SUPPRESS]
    if opts:
        subheading("Options")
        emit(rows(opts))
    if p.epilog:
        emit(Text(""))
        for line in p.epilog.rstrip().splitlines():
            emit(_backticks(line))
    close_box()


# ---------------------------------------------------------------- main

EPILOG = """\
Typical day:
  `agent pull`             see what the agent did
  `agent review <task>`    read its handoff, open the changes in VS Code
  `agent merge <task>`     take the changes into your repo, staged

Disk space: `agent storage`.  Tab completion: `agent completion install`.  New version: `agent update`.
"""


def build_parser() -> argparse.ArgumentParser:
    ap = AgentParser(prog="agent", description="Host CLI for the Claude Code agent container.",
                     epilog=EPILOG, formatter_class=HELP_FORMAT)
    ap._positionals.title = "Commands"
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")
    add_parser = sub.add_parser
    sub.add_parser = lambda *args, **kw: add_parser(*args, formatter_class=HELP_FORMAT, **kw)

    s = sub.add_parser("setup", help="set up this machine: folders, Claude config, vault, image, container")
    s.add_argument("--claude-version", help="Claude Code version for the image (default: pinned, else latest)")
    s.add_argument("--no-build", action="store_true", help="skip building the image")
    s.add_argument("--no-start", action="store_true", help="do not start the container")
    s.add_argument("--home", metavar="PATH",
                   help="where the agent keeps its data (default: asks, suggesting the disk with most space)")
    s.set_defaults(fn=cmd_setup)

    for cmd, fn, help_ in (("up", lambda a: compose("up", "-d"), "start the container"),
                           ("down", lambda a: compose("stop"), "stop the container (keeps everything)"),
                           ("restart", lambda a: compose("restart"), "restart the container"),
                           ("logs", lambda a: compose("logs", "-f", "--tail", "100"), "follow container logs"),
                           ("login", lambda a: exec_in("claude", "auth", "login"), "log in to Claude inside the container (once per machine)"),
                           ("attach", lambda a: exec_in("tmux", "attach", "-t", "agent"), "see the Remote Control sessions (tmux)"),
                           ("shell", lambda a: exec_in("bash"), "open a shell in the container"),
                           ("doctor", cmd_doctor, "check that everything is healthy"),
                           ("list", cmd_list, "list shared repos")):
        sub.add_parser(cmd, help=help_).set_defaults(fn=fn)

    s = sub.add_parser("checkup", help="report what the agent has been doing, and anything risky (changes nothing)")
    s.add_argument("--all", action="store_true", help="look at all history, not just since the last checkup")
    s.add_argument("--no-save", action="store_true", help="don't remember this as the last checkup")
    s.set_defaults(fn=cmd_checkup)

    s = sub.add_parser("update", help="update this tool (and optionally Claude Code), rebuilding only what changed")
    s.add_argument("--check", action="store_true", help="only show what's new")
    s.add_argument("--claude", action="store_true", help="also upgrade Claude Code in the image to the newest")
    s.add_argument("--reinstall", action="store_true", help="re-run install.sh even if nothing changed")
    s.add_argument("--no-install", action="store_true", help="update the files only; run install.sh yourself later")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("share", help="give the agent a repo (dry run unless --yes)")
    s.add_argument("path", nargs="?", default=".", help="the repo to share (default: the one you're in)")
    s.add_argument("--name", help="project name (default: folder name)")
    s.add_argument("--yes", action="store_true", help="actually share")
    s.add_argument("--no-hooks", action="store_true", help="don't install auto-push hooks")
    s.set_defaults(fn=cmd_share)

    s = sub.add_parser("push", help="send your commits to the agent (runs automatically after commits)")
    s.add_argument("--wip", action="store_true", help="also send everything you haven't committed (secrets and .gitignore'd files are left out)")
    s.add_argument("--quiet", action="store_true", help="print nothing (used by the auto-push hooks)")
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser("pull", help="fetch the agent's branches into your repo")
    s.add_argument("task", nargs="?", help="with --overwrite: the task to take (Tab completes it)")
    s.add_argument("--overwrite", action="store_true",
                   help="make your files exactly match the task's branch, discarding your uncommitted "
                        "changes (they're saved first)")
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("review", help="show a task's handoff and open its changes in VS Code")
    s.add_argument("task", help="the task name, e.g. uart-fix (Tab completes it)")
    s.add_argument("--base", default="HEAD", help="compare against this ref (default: your HEAD)")
    s.add_argument("--no-open", action="store_true", help="don't open VS Code")
    s.add_argument("--close", action="store_true", help="remove the review copy")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("merge", help="take a task's changes into your repo as your own (staged, not committed)")
    s.add_argument("task", help="the task name, e.g. uart-fix (Tab completes it)")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                      help="also commit them as you, with the pre-filled message")
    mode.add_argument("--merge", action="store_true",
                      help="a real git merge instead, keeping the agent's commits in your history")
    # Older spellings of the default behaviour.
    mode.add_argument("--squash", "--edit", action="store_true", dest="legacy", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("unshare", help="stop sharing a repo and delete the agent's copy")
    s.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(fn=cmd_unshare)

    for name in ("push", "pull", "review", "merge", "unshare"):
        sub.choices[name].add_argument("--project", help="shared repo name (default: the repo you're in)")

    s = sub.add_parser("storage", help="disk space used by the agent and Docker; clean up or move it")
    s.set_defaults(fn=cmd_storage)
    st = s.add_subparsers(dest="storage_cmd", metavar="ACTION")
    st_add = st.add_parser
    st.add_parser = lambda *args, **kw: st_add(*args, formatter_class=HELP_FORMAT, **kw)
    c = st.add_parser("clean", help="remove old agent images left by rebuilds")
    c.add_argument("--build-cache", action="store_true", help="also clear Docker's build cache (all projects)")
    c.add_argument("--yes", action="store_true", help="don't ask")
    c.set_defaults(fn=cmd_storage_clean)
    c = st.add_parser("move", help="move the agent's data to another folder or disk")
    c.add_argument("target", metavar="PATH", help="new folder, e.g. /data/Agent (must be empty or missing)")
    c.add_argument("--yes", action="store_true", help="don't ask")
    c.set_defaults(fn=lambda a: cmd_storage_move(argparse.Namespace(path=a.target, yes=a.yes)))
    c = st.add_parser("docker-move", help="show the commands to move Docker's own data to another disk")
    c.add_argument("target", metavar="PATH", help="new folder for Docker's data, e.g. /data/docker")
    c.set_defaults(fn=lambda a: cmd_docker_move(argparse.Namespace(path=a.target)))

    s = sub.add_parser("completion", help="shell tab completion: install it, or print the script")
    s.add_argument("action", choices=["install", "bash", "zsh", "fish"],
                   help="install: for your shell ($SHELL); or print the script for one shell")
    s.add_argument("--shell", choices=["bash", "zsh", "fish"], help="with install: this shell instead of $SHELL")
    s.set_defaults(fn=cmd_completion)
    return ap


def tilde(path: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def welcome() -> None:
    """`agent` with no command: who/where, then the main commands."""
    reg = load_registry()
    up = shutil.which("docker") is not None and container_running()
    banner([("agent", "bold"),
            (f"{tilde(AGENT_HOME)}  ·  {len(reg)} shared repo{'s' * (len(reg) != 1)}", "muted"),
            ("● container running", "ok") if up else ("○ container stopped  (agent up)", "muted")])
    heading("Commands")
    for cmd, what in (("agent pull", "see what the agent did"), ("agent review <task>", "handoff + changes in VS Code"),
                      ("agent merge <task>", "take the changes, staged"), ("agent share PATH --yes", "give it a repo"),
                      ("agent storage", "disk space"), ("agent doctor", "health check"),
                      ("agent checkup", "has it behaved?"),
                      ("agent update", "get the newest version"), ("agent --help", "everything")):
        if OUT is None:
            print(f"  {cmd.ljust(24)}{what}")
        else:
            emit(Text.assemble(("  " + cmd.ljust(24), "cmd"), (what, "muted")))
    close_box()


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["__complete"]:
        cmd_complete(argv[1:])
        return
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        welcome()
        return
    try:
        a.fn(a)
    finally:
        close_box()


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
