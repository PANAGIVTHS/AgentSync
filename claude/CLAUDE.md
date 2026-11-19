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
- Search before reading: `rg`, Glob, ranged reads. Don't read whole files unless you need all of them.
- Run builds, test suites and anything that may print more than ~30 lines as `quiet <cmd>` (or `quiet 'a && b'`). It keeps the full log and prints status, summary, first distinct errors and the tail. Dig further with `rg -n PATTERN <log>` or a ranged read, never by printing the whole log.
- Broad questions across many files (where is X handled, what uses Y): ask the Explore subagent and keep only its conclusion. For a known file or symbol, search yourself.
- First time building or testing a repo: `vault find <project> build test`. If there's no note, save one once tests run (commands, setup, gotchas).
- Write scripts for repeated multi-step work instead of repeating tool calls.
- Keep replies short: what changed, how it was verified, what is left.
