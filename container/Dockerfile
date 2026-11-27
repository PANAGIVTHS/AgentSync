# The agent's runtime. Tools that need no special setup go in packages.txt.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ripgrep tmux ca-certificates curl python3 python3-pip python3-venv less procps openssh-client xz-utils \
 && rm -rf /var/lib/apt/lists/*

COPY packages.txt /tmp/packages.txt
RUN pkgs="$(grep -vE '^[[:space:]]*(#|$)' /tmp/packages.txt | tr '\n' ' ')"; \
    if [ -n "$pkgs" ]; then \
      apt-get update && apt-get install -y --no-install-recommends $pkgs && rm -rf /var/lib/apt/lists/*; \
    fi

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force

COPY bin/ /opt/agent/bin/
COPY githooks/ /opt/agent/githooks/
RUN chmod -R a+rX /opt/agent && chmod a+x /opt/agent/bin/* /opt/agent/githooks/* \
 && mkdir -p /home/agent /work /mirrors /handoffs /vault /tools && chmod 1777 /home/agent

# /tools is the agent's permanent tools folder (see tools-template/README.md).
# Its PATH entries are fixed here because Claude Code freezes PATH when a chat
# starts; they come after the system's, so a tool can't replace a system command.
# Everything else is read from /tools/env.sh before every Bash command (BASH_ENV).
ENV PATH=/opt/agent/bin:$PATH:/tools/bin:/tools/python/bin:/tools/npm/bin:/tools/jdks/current/bin \
    BASH_ENV=/tools/env.sh \
    AGENT_TOOLS=/tools \
    PYTHONUSERBASE=/tools/python \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_CACHE_DIR=/tools/caches/pip \
    NPM_CONFIG_PREFIX=/tools/npm \
    NPM_CONFIG_CACHE=/tools/caches/npm \
    UV_CACHE_DIR=/tools/caches/uv \
    UV_TOOL_DIR=/tools/opt/uv-tools \
    UV_TOOL_BIN_DIR=/tools/bin \
    UV_PYTHON_INSTALL_DIR=/tools/opt/uv-python \
    GRADLE_USER_HOME=/tools/caches/gradle \
    HOME=/home/agent \
    DISABLE_AUTOUPDATER=1 \
    AGENT_VAULT=/vault \
    AGENT_HANDOFFS=/handoffs
WORKDIR /work
ENTRYPOINT ["/opt/agent/bin/agent-supervisor"]
