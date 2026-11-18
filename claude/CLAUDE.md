# Agent environment

You run in a Docker container. The user talks to you from the Claude Code web UI or app, one chat at a time, and works on their own copy of the repo in parallel.

## Repos and git
- Each shared repo is your own clone at `/work/<project>`. Remote `desktop` is a read-only mirror of the user's branches: `git fetch desktop` shows their latest work and full history (`desktop/<branch>`). `desktop-wip/<branch>` holds their uncommitted work when they send it.
- Work only on `agent/<task>` branches. Before your first change for a request, if you aren't already on a suitable `agent/` branch, run `agent-task start <short-name>` yourself (add `--from <branch>` if the user names one). The user won't type /task. Commit in small steps with clear messages. A hook rejects commits on other branches.
- Never push. There are no credentials and pushes are blocked. The user pulls your branches, reviews, merges and pushes.
- When a request is done, follow the handoff skill yourself: commit, run `agent-task handoff`, write the summary.

## Memory: the vault at /vault
- Before non-trivial work, run `vault find <keywords>` and read only the notes that matter. A hook may list matching notes under the user's prompt; open them only if useful.
- At handoff, save what will still be true later (see the vault skill). Fix or delete a note as soon as you find it wrong. Never store secrets.

## Token economy
- Search before reading: `rg`, Glob, ranged reads. Don't read whole trees.
- Send long command output to a file under /tmp and read only the part you need.
- Write scripts for repeated multi-step work instead of repeating tool calls.
- Keep replies short: what changed, how it was verified, what is left.
