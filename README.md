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

`install.sh` creates a small Python environment for the CLI (`~/.local/share/agent/venv`, with Rich for tables and coloured output). If that isn't possible, for example on Ubuntu without `python3-venv`, the CLI still works with plain output. It then puts `agent` on your PATH (`~/.local/bin`) and runs `agent setup`, which:
- creates `~/Agent/`;
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
agent merge uart-fix       # real git merge; conflicts open in VS Code's merge editor
git push                   # you push, from your account
```

**Small fixes yourself.** A rename or a tweak doesn't need another round with the agent. Use:

```bash
agent merge uart-fix --edit
```

This starts the merge without committing. The agent's changes are staged in your repo; make your fixes in VS Code, then `git commit`. `git merge --abort` undoes it all.

You can also edit the review copy that `agent review` opens. Your edits are never thrown away:
- `agent merge` (with or without `--edit`) applies them in your repo.
- Running `agent review` again carries them into the fresh copy.
- `agent review <task> --close` saves them as a patch and tells you where.

`--squash` stages the whole task as one change instead of a merge.

`agent push --wip` sends your uncommitted tracked changes too; the agent sees them as `desktop-wip/<branch>`. Other commands:
- `agent list` and `agent doctor` show what's shared and whether everything is healthy.
- `agent up`, `agent down` and `agent logs` start, stop and show the container.
- `agent shell` opens a shell inside it.
- `agent unshare --project NAME` stops sharing a repo.

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
| `~/.claude/CLAUDE.md` | About 450 tokens, once per session, cached after |
| 4 skills | Only their one-line descriptions load until used |
| Push guard hook | 0 (a script; prints only when it blocks a push) |
| Vault hint hook | 0 unless a note strongly matches the prompt |
| Remote Control | 0 |

There are no background agents, no automatic tidy passes, and no hooks that make Claude take extra turns.

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
cli/agent.py              host CLI (Rich output when available, plain otherwise)
requirements.txt          Rich + rich-argparse for the CLI's venv
container/                Dockerfile, compose, packages.txt, gitconfig
container/bin/            in-container tools: agent-task, vault, agent-guard, agent-supervisor
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
