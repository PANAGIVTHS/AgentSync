# The agent's runtime. Tools that need no special setup go in packages.txt.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ripgrep tmux ca-certificates curl python3 less procps openssh-client \
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
 && mkdir -p /home/agent /work /mirrors /handoffs /vault && chmod 1777 /home/agent

ENV PATH=/opt/agent/bin:$PATH \
    HOME=/home/agent \
    DISABLE_AUTOUPDATER=1 \
    AGENT_VAULT=/vault \
    AGENT_HANDOFFS=/handoffs
WORKDIR /work
ENTRYPOINT ["/opt/agent/bin/agent-supervisor"]
