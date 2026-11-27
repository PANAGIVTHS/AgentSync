# Agent tools

Programs the agent downloads for its tasks. This folder is `/tools` in the container and `~/Agent/tools` on your PC. It's kept across restarts, rebuilds and updates, and it's never shared into your repos. Your PC never runs anything from it.

| Folder | What goes in it |
|---|---|
| `bin/` | Programs and small launcher scripts. On PATH in every shell. |
| `opt/<tool>/` | Unpacked toolchains, e.g. `opt/oss-cad-suite/`. Link or launch their programs from `bin/`. |
| `sysroot/<tool>/` | Libraries a program needs (unpacked `.deb` files). Only that program's launcher points at them. |
| `jdks/` | Java installs. `jdks/current` links to the active one; `JAVA_HOME` follows it. |
| `python/` | `pip install --user` lands here; its `bin/` is on PATH. |
| `npm/` | `npm install -g` lands here; its `bin/` is on PATH. |
| `downloads/` | Archives fetched with `tools fetch`, checksum verified. |
| `caches/` | pip, uv, npm and Gradle caches. Safe to delete. |
| `env.d/<tool>.sh` | Extra settings for one tool, read before every command. Quiet, no PATH changes. |

`manifest.json` records every install: name, version, where it came from, its checksum, and which repo needed it. The agent keeps it with `tools record`. `agent checkup` on your PC compares the folder with it, and on a new PC the agent can reinstall everything from it.
