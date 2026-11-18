# Agent memory

This Obsidian vault is the agent's long-term memory. The agent writes and maintains it itself; you can read and edit it in Obsidian at any time.

- `topics/`: general knowledge (tools, languages, techniques).
- `projects/<repo>/`: knowledge about one shared repo.
- `Index.md`: a map of every note, rebuilt automatically on each change.

Each note's **tags** are its search keywords. The agent runs `vault find <keywords>` before work, and a hook lists strongly matching notes under each prompt. Only matching notes are read, so the vault costs nothing until it's relevant.

Every change is committed to this folder's git history, so you can see what the agent changed and undo it. Avoid Obsidian plugins that run git in this folder: it's written from inside the container.
