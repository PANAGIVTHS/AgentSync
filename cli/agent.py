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
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT_HOME = Path(os.environ.get("AGENT_HOME", str(Path.home() / "Agent"))).expanduser()
IMAGE = os.environ.get("AGENT_IMAGE", "agent-runtime:local")
CONTAINER = "agent"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
HOOK_BEGIN = "# >>> agent auto-push >>>"
HOOK_END = "# <<< agent auto-push <<<"
HOOK_NAMES = ("post-commit", "post-merge", "post-rewrite")
NO_PUSH = "no-push://disabled"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]")


class Fail(Exception):
    """A user-facing error, printed without a traceback."""


# ---------------------------------------------------------------- output
# Rich when available (installed by install.sh into the CLI's venv); plain text
# otherwise or with AGENT_PLAIN=1. Messages are passed as Text, never as markup,
# so brackets in branch names or agent-written text print literally.

try:
    if os.environ.get("AGENT_PLAIN") == "1":
        raise ImportError
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    OUT = Console(highlight=False, soft_wrap=True)
    ERR = Console(stderr=True, highlight=False, soft_wrap=True)
except ImportError:
    OUT = ERR = None

try:
    if OUT is None:
        raise ImportError
    from rich_argparse import RichHelpFormatter as HELP_FORMAT
except ImportError:
    HELP_FORMAT = argparse.HelpFormatter


def _say(console, plain_prefix: str, rich_prefix: tuple[str, str], msg: str, style: str = "", err=False) -> None:
    if console is None:
        print(f"{plain_prefix}{msg}", file=sys.stderr if err else sys.stdout)
    else:
        console.print(Text.assemble(rich_prefix, (msg, style)))


def ok(msg: str) -> None:
    _say(OUT, "✓ ", ("✓ ", "bold green"), msg)


def note(msg: str) -> None:
    _say(OUT, "  ", ("  ", ""), msg, "dim")


def warn(msg: str) -> None:
    _say(ERR, "! ", ("! ", "bold yellow"), msg, err=True)


def error(msg: str) -> None:
    _say(ERR, "✗ ", ("✗ ", "bold red"), msg, err=True)


def heading(msg: str) -> None:
    if OUT is None:
        print(msg)
    else:
        OUT.print(Text(msg, style="bold cyan"))


def table(columns: list[str], rows: list[list[str]], styles: list[str] | None = None) -> None:
    """A borderless table; plain mode aligns columns with spaces."""
    if OUT is None:
        widths = [max(len(str(r[i])) for r in [columns, *rows]) for i in range(len(columns))]
        for r in [columns, *rows]:
            print("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
        return
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, header_style="bold", pad_edge=False)
    for i, c in enumerate(columns):
        t.add_column(c, style=(styles or [""] * len(columns))[i], overflow="fold")
    for r in rows:
        t.add_row(*[Text(str(c)) for c in r])
    OUT.print(t)


def panel(text: str, title: str) -> None:
    if OUT is None:
        print(f"── {title} ──\n{text}\n")
    else:
        OUT.print(Panel(Text(text), title=title, title_align="left", border_style="dim", expand=False))


def sanitize(text: str) -> str:
    """Agent-written text is printed to your terminal: drop escape sequences."""
    return CONTROL.sub("", text)


def clean_env() -> dict:
    # Git hooks export GIT_DIR / GIT_INDEX_FILE. Never let them leak into the
    # git commands we run against other repositories.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run(cmd, *, check=True, capture=True):
    cmd = [str(c) for c in cmd]
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
    names = ("home", "work", "mirrors", "handoffs", "vault", "review")
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
    note(f"Your repo:    {src}")
    note(f"Branches:     {len(branches)} (default {default}); committed history only")
    note(f"Agent clone:  /work/{name} in the container")
    heading("Changes to your repo")
    note("+ remote 'agent' (fetch only; pushing to it is disabled)")
    if hooks_ok:
        note("+ hooks post-commit, post-merge, post-rewrite: auto-send your commits")
    elif a.no_hooks:
        note("  no hooks (--no-hooks): run `agent push` after you commit")
    else:
        note("  no hooks (core.hooksPath is set): run `agent push` after you commit")
    if not a.yes:
        print("Dry run. Re-run with --yes to share.")
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
    subjects = [s for s in out(src, "log", "--reverse", "--no-merges", "--format=%s", f"{base}..{sha}").splitlines()
                if s and not s.startswith("wip: autosave")]
    if len(subjects) == 1:
        text = subjects[0] + "\n"
    else:
        title = task.replace("-", " ").replace("_", " ").strip().capitalize() or task
        text = title + "\n\n" + "".join(f"- {s}\n" for s in subjects)
    return TRAILER.sub("", sanitize(text))


def cmd_push(a) -> None:
    name, src = resolve_project(a.project)
    push_mirror(src, name)
    settle(src, name)
    if a.wip:
        branch = out(src, "branch", "--show-current") or "detached"
        sha = out(src, "stash", "create")
        if sha:
            git(src, "push", "--quiet", "--force", "--no-verify", mirror_of(name), f"{sha}:refs/wip/{branch}")
            if not a.quiet:
                ok(f"sent uncommitted changes as desktop-wip/{branch} (untracked files not included)")
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


def cmd_pull(a) -> None:
    name, src = resolve_project(a.project)
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
        rows.append([f"agent/{task}", task_state(src, name, task), ready, subject])
    heading(f"Agent branches in {name}")
    table(["Branch", "Commits", "Handoff", "Last commit"], rows, ["bold green", "", "", "dim"])
    note("Review: agent review <task>    Take the changes: agent merge <task>")


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
        base, label = out(src, "merge-base", a.base, ref), f"against {a.base}"
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
    stat = out(src, "diff", "--stat", base, ref).splitlines()
    if stat:
        note(stat[-1].strip())
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
    note(f"Take the changes: agent merge {task}    Close: agent review {task} --close")


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
        base = out(src, "merge-base", "HEAD", sha)
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
    note(f"  {message.splitlines()[0]}")
    note("To undo: git reset --merge")


def cmd_list(_a) -> None:
    reg = load_registry()
    if not reg:
        print("No shared repos. Share one: agent share PATH --yes")
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
        rows.append([name, str(src), ", ".join(flags) or "ok"])
    heading(f"Shared repos · {len(rows)}")
    table(["Project", "Your repo", "Status"], rows, ["bold green", "cyan", ""])


def cmd_unshare(a) -> None:
    name, src = resolve_project(a.project)
    pending = []
    if src.is_dir() and git(src, "remote", "get-url", "agent", check=False).returncode == 0:
        git(src, "fetch", "--prune", "--quiet", "agent", check=False)
        pending = [t for t in agent_tasks(src) if task_state(src, name, t) != "merged"]
    print(f"Unshare {name}: deletes the mirror, the agent's clone and handoffs; removes the remote and hooks from your repo.")
    if pending:
        warn(f"unmerged agent branches will be lost: {', '.join(pending)}")
    if not a.yes:
        if input(f"Type {name} to confirm: ").strip() != name:
            print("Cancelled.")
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
    for key in ("home", "work", "mirrors", "handoffs", "vault"):
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


def machine_file() -> Path:
    return AGENT_HOME / "machine.json"


def build_image(version: str | None) -> None:
    info = json.loads(machine_file().read_text()) if machine_file().exists() else {}
    version = version or info.get("claude_code") or "latest"
    print(f"Building {IMAGE} with Claude Code {version} …")
    run(["docker", "build", "--build-arg", f"CLAUDE_CODE_VERSION={version}", "-t", IMAGE,
         REPO / "container"], capture=False)
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


def cmd_setup(a) -> None:
    if not a.no_build or not a.no_start:
        preflight()
    for d in dirs().values():
        d.mkdir(parents=True, exist_ok=True)
    write_env()
    shutil.copy2(REPO / "container" / "compose.yaml", AGENT_HOME / "compose.yaml")
    install_claude_config()
    install_gitconfig()
    init_vault()
    if not a.no_build:
        build_image(a.claude_version)
    if not a.no_start:
        compose("up", "-d")
    ok(f"set up in {AGENT_HOME}")
    note("Next (once per machine): agent login    Then: agent share PATH --yes")


def cmd_doctor(_a) -> None:
    bad = 0

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal bad
        tail = "" if good or not hint else f"  → {hint}"
        if OUT is None:
            print(f"  {'✓' if good else '✗'} {label}{tail}")
        else:
            OUT.print(Text.assemble(("  ✓ " if good else "  ✗ ", "bold green" if good else "bold red"),
                                    label, (tail, "yellow")))
        bad += not good

    heading("agent doctor")

    docker = shutil.which("docker") is not None
    check("git installed", shutil.which("git") is not None)
    check("docker installed", docker)
    if docker:
        check("docker daemon reachable", run(["docker", "info"], check=False).returncode == 0)
        check("image built", run(["docker", "image", "inspect", IMAGE], check=False).returncode == 0, "agent setup")
        check("container running", container_running(), "agent up")
    check("set up", (AGENT_HOME / "compose.yaml").exists(), "agent setup")
    if docker and container_running():
        status = run(["docker", "exec", CONTAINER, "claude", "auth", "status"], check=False).stdout
        check("logged in to Claude", bool(re.search(r'"loggedIn":\s*true', status)), "agent login")
    check("vault exists", (dirs()["vault"] / ".git").is_dir(), "agent setup")
    launcher = shutil.which("agent")
    me = str(Path(__file__).resolve())
    mine = launcher is not None and (Path(launcher).resolve() == Path(me)
                                     or me in Path(launcher).read_text(errors="replace"))
    check("`agent` on PATH", mine, "run ./install.sh, or add ~/.local/bin to PATH")
    check("pretty output (Rich)", OUT is not None or os.environ.get("AGENT_PLAIN") == "1",
          "re-run ./install.sh (Ubuntu may need: sudo apt install python3-venv)")
    for name, info in sorted(load_registry().items()):
        src = Path(info["source"])
        url = git(src, "remote", "get-url", "agent", check=False).stdout.strip() if src.is_dir() else ""
        check(f"{name}: repo, mirror, clone, remote",
              src.is_dir() and mirror_of(name).is_dir() and work_of(name).is_dir() and url == str(work_of(name)),
              f"agent unshare {name} --yes, then share again")
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- main

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="agent", description="Host CLI for the Claude Code agent container.",
                                 formatter_class=HELP_FORMAT)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    add_parser = sub.add_parser
    sub.add_parser = lambda *args, **kw: add_parser(*args, formatter_class=HELP_FORMAT, **kw)

    s = sub.add_parser("setup", help="set up this machine: folders, Claude config, vault, image, container")
    s.add_argument("--claude-version", help="Claude Code version for the image (default: pinned, else latest)")
    s.add_argument("--no-build", action="store_true", help="skip building the image")
    s.add_argument("--no-start", action="store_true", help="do not start the container")
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

    s = sub.add_parser("share", help="give the agent a repo (dry run unless --yes)")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--name", help="project name (default: folder name)")
    s.add_argument("--yes", action="store_true", help="actually share")
    s.add_argument("--no-hooks", action="store_true", help="don't install auto-push hooks")
    s.set_defaults(fn=cmd_share)

    s = sub.add_parser("push", help="send your commits to the agent (runs automatically after commits)")
    s.add_argument("--wip", action="store_true", help="also send uncommitted tracked changes")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_push)

    s = sub.add_parser("pull", help="fetch the agent's branches into your repo")
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("review", help="show a task's handoff and open its changes in VS Code")
    s.add_argument("task")
    s.add_argument("--base", default="HEAD", help="compare against this ref (default: your HEAD)")
    s.add_argument("--no-open", action="store_true", help="don't open VS Code")
    s.add_argument("--close", action="store_true", help="remove the review copy")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("merge", help="take a task's changes into your repo as your own (staged, not committed)")
    s.add_argument("task")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                      help="also commit them as you, with the pre-filled message")
    mode.add_argument("--merge", action="store_true",
                      help="a real git merge instead, keeping the agent's commits in your history")
    # Older spellings of the default behaviour.
    mode.add_argument("--squash", "--edit", action="store_true", dest="legacy", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("unshare", help="stop sharing a repo and delete the agent's copy")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_unshare)

    for name in ("push", "pull", "review", "merge", "unshare"):
        sub.choices[name].add_argument("--project", help="shared repo name (default: the repo you're in)")

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
