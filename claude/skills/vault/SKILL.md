---
name: vault
description: Long-term memory in the Obsidian vault at /vault. Use to look up what earlier sessions learned before non-trivial work, and to save, correct or remove durable knowledge.
---

# Looking things up
- `vault find <keywords>` lists the best matches (path, title, tags, first line). Use 2–4 specific words: module names, tools, error terms.
- `vault show <path>` prints one note. Read only what is relevant.

# What to save, and where
Save only what will still be true next month and saves time: how a subsystem works, a decision and its reason, a non-obvious fix, a command that works.
- **`--project <repo>`** for facts about one repo. **Omit it** (goes to `topics/`) for anything true across repos: a tool, a language, a technique. If you'd write the same thing in a second repo's note, it belongs in topics.
- **Not in the vault:**
  - How you installed a tool, or "X isn't in the container": install it into /tools and `tools record` it. The manifest is the record.
  - Results of one run: timings, numbers, "took 3 min". Keep the method and the lasting conclusion.
  - Anything about this session or chat.
  - Personal details beyond what the work needs.
  - Secrets, ever.
  - What the code or git history already says.
- **Shape:** one fact per bullet, under 40 lines. Link related notes with [[note-name]].

```bash
vault add "UART RX framing at high baud rates" --project <project> --tags uart,timing,rx --verified <short-sha> --stdin <<'EOF'
- Sample at 16x; 921600 baud needs a 14.7456 MHz clock.
- See [[uart-clocking]] for the divider table.
EOF
```
- Tags are the search keywords: lowercase, 2–5 per note, specific words you would search for (`verilator`, `lsu`), not generic ones (`tests`, `build`).
- If `add` lists similar notes, update one of them instead.
- If `add` or `update` prints `check:` lines, fix the note right away.

# Keeping it correct
- `vault update <path> --stdin` (replace body), `--append "text"`, `--add-tags a,b`, `--verified <sha>`.
- If a note turns out wrong, fix it or `vault rm <path>` right away.
- `vault lint` lists unfindable, stale, long or duplicate notes, and notes holding things that don't belong (see above). Fix what it reports when you are already updating the vault, or when the user asks you to tidy it.
- Every change is committed to the vault's own git history automatically.
