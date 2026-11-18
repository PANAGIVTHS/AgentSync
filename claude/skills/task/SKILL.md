---
name: task
description: Start or resume a task branch. Use when the user types /task <name> [from <branch>] <goal>, or asks to start, switch or resume a task.
---

1. Parse the request: the first word is the task name; `from <branch>` (optional) is the user's branch to start from; the rest is the goal.
2. From the project root run `agent-task start <name>` (add `--from <branch>` if given). It fetches the user's latest commits, autosaves uncommitted work on the previous task, and switches to `agent/<name>`.
3. If it prints an error, tell the user and stop. Don't work around it.
4. Run `vault find` with 2–4 keywords from the goal and read only the useful notes.
5. Work on the goal. Commit in small steps on this branch. When done, use /handoff.
