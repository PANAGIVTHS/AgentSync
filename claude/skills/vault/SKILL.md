---
name: vault
description: Long-term memory in the Obsidian vault at /vault. Use to look up what earlier sessions learned before non-trivial work, and to save, correct or remove durable knowledge.
---

# Looking things up
- `vault find <keywords>` lists the best matches (path, title, tags, first line). Use 2–4 specific words: module names, tools, error terms.
- `vault show <path>` prints one note. Read only what is relevant.

# Saving
Save only what will still be true later and would save time next session: how a subsystem works, a decision and its reason, a non-obvious fix, a command that works. Not task logs, not things the code or git history already say.

```bash
vault add "UART RX framing at high baud rates" --project <project> --tags uart,timing,rx --verified <short-sha> --stdin <<'EOF'
One fact per line or a short paragraph. Under 30 lines.
Link related notes with [[note-name]].
EOF
```
- `--project` for repo-specific notes; omit it for general knowledge (tools, languages).
- Tags are the search keywords: lowercase, 2–5 per note, the words you would search for.
- If `add` lists similar notes, update one of them instead.

# Keeping it correct
- `vault update <path> --stdin` (replace body), `--append "text"`, `--add-tags a,b`, `--verified <sha>`.
- If a note turns out wrong, fix it or `vault rm <path>` right away.
- `vault lint` lists unfindable, stale, long or duplicate notes; fix what it reports when you are already updating the vault.
- Every change is committed to the vault's own git history automatically. Never put secrets in the vault.
