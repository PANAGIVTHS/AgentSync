# agent

A Claude Code agent that lives in one Docker container on your machine. You give it tasks from the Claude Code web UI or app; it works on its own copy of your repos and commits on `agent/<task>` branches. You review and merge in VS Code, and push from your own account. The agent cannot push anywhere.

```text
Browser / app (claude.ai/code) ──Remote Control──▶ agent container
                                                   /work/<repo>   the agent's clone; task = branch agent/<task>
                                                   /vault         Obsidian memory, searched by keyword
Your repo ──agent push (automatic after commits)──▶ read-only mirror ──▶ the agent's clone
Your repo ◀──agent pull (fetches agent/* only)──────────────────────── the agent's clone
```

## Install on a machine

Needs Linux (or macOS) with Docker Engine + Compose, git, and Python 3.9+. Your user must be able to run `docker` without sudo.

```bash
git clone <this repo> ~/agent-setup
cd ~/agent-setup
./install.sh
agent login
```

`install.sh` creates a small Python environment for the CLI (`~/.local/share/agent/venv`, with Rich for tables and coloured output). If that isn't possible, for example on Ubuntu without `python3-venv`, the CLI still works with plain output. It then puts `agent` on your PATH (`~/.local/bin`), installs tab completion for your shell, and runs `agent setup`, which:
- asks where to keep the agent's data the first time, listing each disk with its free space and suggesting the one with the most (`./install.sh --home /data/Agent` skips the question). The choice is saved in `~/.config/agent/config.json`;
- installs the agent's Claude config;
- creates the Obsidian vault;
- builds the image, pinning the Claude Code version it installed;
- starts the container.

`agent login` signs the container in to your Claude subscription, once per machine. Re-run `./install.sh` any time to update. It keeps your login, vault, and shared repos.

## Share a repo

```bash
agent share ~/Projects/fpga-cam          # dry run: shows exactly what it will do
agent share ~/Projects/fpga-cam --yes
agent attach                             # first time only: accept the folder trust prompt in window rc-fpga-cam
```

Sharing:
- **Creates a read-only mirror** of your branches. Only committed history goes in, never your working files.
- **Gives the agent its own clone** of that mirror.
- **Adds a fetch-only remote `agent`** to your repo.
- **Adds hooks** that send each commit to the mirror automatically.

The repo then shows up in claude.ai/code as `fpga-cam`.

## Daily use

In the browser, open the repo and start a session:

| Type | What happens |
|---|---|
| `/task uart-fix fix RX framing at 921600` | Branch `agent/uart-fix` from your default branch, then it works |
| `/task hls from feat/dsp pipeline the FIR` | Same, starting from your `feat/dsp` |
| `/status` | Task branches, what's ready for review |
| `/handoff` | Rebases onto your latest work, writes a short review summary, saves what it learned |

In your terminal, from inside your repo:

```bash
agent pull                 # fetch the agent's branches (as agent/<task>); your files are untouched
agent review uart-fix      # summary + commits, then VS Code with every change shown as staged
agent merge uart-fix       # the agent's changes appear in your repo as staged files
```

Then commit in VS Code (or `git commit`) and `git push`, from your account.

**It works like HermesSync's sync.** The agent only changes files; you take them into your repo as your own work:
- `agent merge` stages the agent's changes. It doesn't commit, and it doesn't bring the agent's commits into your history.
- You check them in VS Code, fix anything you like (a rename doesn't need another round with the agent), then commit.
- The commit message is pre-filled from the agent's commit titles, with any co-author lines removed. On GitHub it's a normal commit by you: no agent commits, no Claude co-author.
- Conflicts open in VS Code under **Merge Changes**.
- `git reset --merge` undoes the whole thing.

Options:
- `agent merge <task> --commit` stages and commits in one go, with the pre-filled message.
- `agent merge <task> --merge` does a real git merge instead, if you ever want the agent's own commits in your history.

**After you've taken a task.** Once you commit, the task shows as `merged` in `agent pull`. If you give the agent follow-up work on the same task:
- it continues from your latest commit, so nothing is offered twice;
- `agent pull` shows only what's new since your last merge, and `agent review` shows only that;
- `agent merge` takes only that.

**Editing the review copy.** You can also edit the copy that `agent review` opens. Your edits are never thrown away:
- `agent merge` brings them into your repo along with the agent's changes.
- Running `agent review` again carries them into the fresh copy.
- `agent review <task> --close` saves them as a patch and tells you where.

`agent push --wip` sends your uncommitted tracked changes too; the agent sees them as `desktop-wip/<branch>`. Other commands:
- `agent list` and `agent doctor` show what's shared and whether everything is healthy.
- `agent up`, `agent down` and `agent logs` start, stop and show the container.
- `agent shell` opens a shell inside it.
- `agent unshare --project NAME` stops sharing a repo.

## Tab completion

`agent completion install` sets it up for your shell (`install.sh` does this for you). It adds one line to `~/.bashrc` or `~/.zshrc`; fish needs none. Then open a new terminal. Tab completes commands, options, your shared repos, and the agent's task names:
- Task names come straight from the agent's clone, so they complete before `agent pull`.
- They complete from any folder: your repo, the review copy, or anywhere if you share only one repo. Otherwise add `--project NAME`.
- When several things match, each is listed with a description: the task's last commit, or what a command does.
- zsh and fish also colour and group the list. In bash, the part you've typed is highlighted. Set `AGENT_COMPLETION_COLORS=0` to leave your readline colours alone.

`agent completion bash|zsh|fish` prints the script instead, if you'd rather install it yourself.

## Disk space

`agent storage` shows what takes space, and where:
- **The agent's data:** repos it works on, mirrors, review copies, its login and the vault, with free space on that disk.
- **Docker's data:** the agent image, old agent images, the build cache, and the container's own files. Docker keeps these in its own folder (usually `/var/lib/docker`, on your system disk) wherever the agent's data is.

Ways to free space:
- **Old images.** Each rebuild (re-running `./install.sh`) used to leave the previous image behind, about 1 GB each. Builds now remove it. `agent storage clean` removes any left from earlier builds. It only touches images built from this repo.
- **Build cache.** `agent storage clean --build-cache` also clears Docker's build cache. That cache belongs to all your Docker projects, so it asks first.
- **Logs.** Container logs are capped at 30 MB.

Moving things to another disk:
- **The agent's data:** `agent storage move /data/Agent` stops the container, moves everything, updates your repos' `agent` remotes, and starts it again. If the old folder was `~/Agent`, it becomes a link to the new one.
- **Docker's data:** `agent storage docker-move /data/docker` prints the commands that move Docker's data folder. They need root, so you run them yourself.

`agent setup` and every build warn when Docker's disk is low, and `agent doctor` checks it too.

## Memory: the Obsidian vault

`~/Agent/vault` is a normal Obsidian vault: open that folder in Obsidian to browse it. The agent writes and maintains it itself with the `vault` tool:
- **Notes are short and tagged.** Each one is a short Markdown file. Its `tags` (Obsidian tags) are its search keywords.
- **Lookup by keyword.** `vault find <keywords>` ranks notes by tag, title and text matches, and returns one line per note. The agent reads only the notes it needs.
- **Automatic hints, cheaply.** A hook checks each prompt against note tags and titles. On a strong match it lists up to 3 note paths, about 20 tokens each. Otherwise it adds nothing.
- **Kept current.** The agent saves durable lessons at `/handoff`, fixes wrong notes when it finds them, and `vault lint` flags stale, long or duplicate notes.
- **An index for Obsidian.** `Index.md` is rebuilt on every change.
- **History.** Every change is committed to the vault's git history, so you can see or undo anything.

## Usage overhead

This setup is designed to cost about the same as using Claude Code normally:

| Added | Cost |
|---|---|
| `~/.claude/CLAUDE.md` | About 650 tokens, once per session, cached after |
| 4 skills | Only their one-line descriptions load until used |
| Push guard hook | 0 (a script; prints only when it blocks a push) |
| Vault hint hook | 0 unless a note strongly matches the prompt |
| Remote Control | 0 |

There are no background agents, no automatic tidy passes, and no hooks that make Claude take extra turns.

What keeps sessions cheap:
- **`quiet`.** Builds and test runs go through `quiet <cmd>`. It keeps the full log in `/tmp/agent-logs` and shows the agent only the status, the summary ("45 passed, 2 failed"), the first distinct errors with line numbers (Verilator `%Error`, `$fatal`, assertions, tracebacks, compiler errors) and the tail. A 50,000-line simulation log costs about 40 lines.
- **Explore first.** Broad searches go to a read-only search subagent. Only its conclusion enters the chat, not the files it read.
- **Notes per repo.** The first time the agent gets a repo's tests running, it saves how, so later sessions don't work it out again.
- **New chats for new work.** A long chat re-reads all of its history on every turn. After `/handoff`, start a new chat for unrelated work; the vault and the handoff carry the context.

## Safety model

**What prevents pushing.** These are structural, not instructions to the agent:
- The mirror is mounted read-only.
- The container has no git credentials.
- The agent's git config rewrites every push URL to an invalid one.

**Extra checks.** A Bash hook blocks `git push` early and saves a wasted turn. A git hook rejects commits outside `agent/*` branches.

**Your side.** On your side, git only ever **fetches** from the agent's clone. Nothing executes inside agent-controlled git folders, and the remote has push disabled. `agent review` strips terminal escape codes from text the agent wrote.

**What the agent can reach.** It has full access inside its container: its clones, the vault, and its own Claude settings. The worst case is damage to its own copies. Your repo and GitHub stay untouched.

## Adding tools

Add Debian packages to `container/packages.txt`, then run `./install.sh`. Tools that need more (a host-installed Vivado, licences, devices) will get a proper toolkit format once the first one is needed.

## Layout

```text
cli/agent.py              host CLI (Claude-themed Rich output when available, plain otherwise)
requirements.txt          Rich + rich-argparse for the CLI's venv
container/                Dockerfile, compose, packages.txt, gitconfig
container/bin/            in-container tools: agent-task, vault, quiet, agent-guard, agent-supervisor
container/githooks/       pre-commit: commits only on agent/* branches
claude/                   CLAUDE.md, settings.json (hooks), skills: task, handoff, status, vault
vault-template/           starting vault
tests/                    python3 -m unittest discover -s tests
~/Agent/ (per machine)    .env, compose.yaml, home/, work/, mirrors/, handoffs/, vault/, review/
```

## Tests

```bash
python3 -m unittest discover -s tests
```

They use throwaway repos and a temporary `AGENT_HOME`, and never touch `~/Agent` or your projects. The Rich output tests run only when Rich is installed for that Python, e.g. with `~/.local/share/agent/venv/bin/python -m unittest discover -s tests`. Docker and Remote Control aren't exercised by the tests; `agent doctor` checks them on a real machine.
