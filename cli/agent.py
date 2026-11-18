#!/usr/bin/env python3
"""agent: host-side CLI for the Claude Code agent container.

The agent never writes to your repo. You share commits through a read-only
mirror, the agent works on agent/<task> branches in its own clone, and this CLI
fetches those branches back into your repo for review and merge.

Standard library only, so it runs on any machine with Python 3.9+.
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


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def note(msg: str) -> None:
    print(f"  {msg}")


def warn(msg: str) -> None:
    print(f"! {msg}", file=sys.stderr)


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

def push_mirror(src: Path, name: str) -> None:
    mirror = mirror_of(name)
    if not mirror.is_dir():
        raise Fail(f"mirror for {name} is missing. Run: agent doctor")
    lock = AGENT_HOME / f".push-{name}.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
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

    print(f"Share {name}")
    note(f"Your repo:    {src}")
    note(f"Branches:     {len(branches)} (default {default}); committed history only")
    note(f"Agent clone:  /work/{name} in the container")
    print("Changes to your repo:")
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


def cmd_push(a) -> None:
    name, src = resolve_project(a.project)
    push_mirror(src, name)
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
    fetch_agent(src)
    tasks = agent_tasks(src)
    if not tasks:
        ok("no agent branches yet")
        return
    print(f"Agent branches in {name}:")
    for task in tasks:
        ref = f"refs/remotes/agent/{task}"
        new = out(src, "rev-list", "--count", f"HEAD..{ref}")
        subject = sanitize(out(src, "log", "-1", "--format=%s", ref))[:60]
        ready = "handoff" if handoff_of(name, task).exists() else "-"
        state = "merged" if new == "0" else f"{new} new"
        print(f"  agent/{task:<24} {state:>8}  {ready:<8} {subject}")
    note("Review: agent review <task>    Merge: agent merge <task>")


def close_review(src: Path, name: str, task: str) -> bool:
    wt = review_of(name, task)
    if not wt.exists():
        return False
    git(src, "worktree", "remove", "--force", wt, check=False)
    shutil.rmtree(wt, ignore_errors=True)
    git(src, "worktree", "prune")
    return True


def cmd_review(a) -> None:
    name, src = resolve_project(a.project)
    task = normalize_task(a.task)
    if a.close:
        ok("review closed" if close_review(src, name, task) else "no open review")
        return
    fetch_agent(src)
    ref = require_task(src, task)
    base = out(src, "merge-base", a.base, ref)
    print(f"Review agent/{task}  (against {a.base} @ {base[:9]})\n")
    hand = handoff_of(name, task)
    if hand.exists():
        print(sanitize(hand.read_text(errors="replace")).strip() + "\n")
    else:
        note("(no handoff written yet)\n")
    log = out(src, "log", "--oneline", "--no-decorate", f"{base}..{ref}")
    print(sanitize(log) or "  (no commits)")
    stat = out(src, "diff", "--stat", base, ref).splitlines()
    if stat:
        print("  " + stat[-1].strip())
    close_review(src, name, task)
    wt = review_of(name, task)
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(src, "worktree", "add", "--quiet", "--detach", wt, ref)
    # HEAD = base, files = agent's version: Source Control shows the whole task as staged changes.
    git(wt, "reset", "--quiet", "--soft", base)
    print()
    ok(f"review copy: {wt}")
    if not a.no_open and shutil.which("code"):
        subprocess.Popen(["code", "--new-window", str(wt)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        note("opened in VS Code: Source Control lists every change the agent made")
    note(f"Merge: agent merge {task}    Close: agent review {task} --close")


def cmd_merge(a) -> None:
    name, src = resolve_project(a.project)
    task = normalize_task(a.task)
    if out(src, "status", "--porcelain", "--untracked-files=no"):
        raise Fail("your repo has uncommitted changes. Commit or stash them first")
    fetch_agent(src)
    ref = require_task(src, task)
    close_review(src, name, task)
    if a.squash:
        args = ["merge", "--squash", ref]
    else:
        args = ["merge", "--no-ff", "-m", f"Merge agent/{task}", ref]
    p = git(src, *args, check=False, capture=False)
    if p.returncode:
        warn("merge stopped on conflicts. In VS Code: Source Control → Merge Changes → resolve, then `git commit`.")
        note("To undo: git merge --abort")
        sys.exit(1)
    if a.squash:
        ok("changes staged. Review them and `git commit`, then `git push` when ready")
    else:
        ok(f"merged agent/{task}. Push when ready: git push")


def cmd_list(_a) -> None:
    reg = load_registry()
    if not reg:
        print("No shared repos. Share one: agent share PATH --yes")
        return
    for name, info in sorted(reg.items()):
        src = Path(info["source"])
        flags = []
        if not src.is_dir():
            flags.append("repo missing")
        if not work_of(name).is_dir():
            flags.append("agent clone missing")
        if not info.get("hooks"):
            flags.append("manual push")
        print(f"  {name:<20} {src}  {', '.join(flags) or 'ok'}")


def cmd_unshare(a) -> None:
    name, src = resolve_project(a.project)
    pending = []
    if src.is_dir() and git(src, "remote", "get-url", "agent", check=False).returncode == 0:
        git(src, "fetch", "--prune", "--quiet", "agent", check=False)
        pending = [t for t in agent_tasks(src)
                   if out(src, "rev-list", "--count", f"HEAD..refs/remotes/agent/{t}") != "0"]
    print(f"Unshare {name}: deletes the mirror, the agent's clone and handoffs; removes the remote and hooks from your repo.")
    if pending:
        warn(f"unmerged agent branches will be lost: {', '.join(pending)}")
    if not a.yes:
        if input(f"Type {name} to confirm: ").strip() != name:
            print("Cancelled.")
            return
    if src.is_dir():
        for task in agent_tasks(src):
            close_review(src, name, task)
        remove_hooks(src)
        git(src, "remote", "remove", "agent", check=False)
    for p in (mirror_of(name), work_of(name), AGENT_HOME / "handoffs" / name, AGENT_HOME / "review" / name):
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
        print(f"  {'✓' if good else '✗'} {label}" + ("" if good or not hint else f"  → {hint}"))
        bad += not good

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
    check("`agent` on PATH", launcher is not None and Path(launcher).resolve() == Path(__file__).resolve(),
          "run ./install.sh, or add ~/.local/bin to PATH")
    for name, info in sorted(load_registry().items()):
        src = Path(info["source"])
        url = git(src, "remote", "get-url", "agent", check=False).stdout.strip() if src.is_dir() else ""
        check(f"{name}: repo, mirror, clone, remote",
              src.is_dir() and mirror_of(name).is_dir() and work_of(name).is_dir() and url == str(work_of(name)),
              f"agent unshare {name} --yes, then share again")
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- main

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="agent", description="Host CLI for the Claude Code agent container.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

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

    s = sub.add_parser("merge", help="merge a task into your current branch")
    s.add_argument("task")
    s.add_argument("--squash", action="store_true", help="stage the task as one change instead of merging")
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
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
