---
name: handoff
description: Finish the current task for the user's review. Use when the user types /handoff or says the task is done or ready for review.
---

1. Commit any remaining work with a clear message.
2. Run `agent-task handoff`. If it reports rebase conflicts, resolve them, `git add` the files, `git rebase --continue`, and run it again.
3. Write the handoff file it names, using its headings, in under 30 lines: what changed and why, how it was verified (exact commands and results), risks and follow-ups. Don't claim checks you didn't run.
4. Memory: if this task taught something that will still be true later (how a subsystem works, a decision and its reason, a gotcha), save it with the vault skill. Skip it if nothing new was learned.
5. Reply in 3–5 lines: branch, summary, verification, and that the user can run `agent review <task>`.
