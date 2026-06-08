# Roost worker — runs jobs in an isolated container so they don't affect the
# host or each other. Ships with Claude Code (native install, no npm) so it can
# run `kind: claude` agent jobs; auth is provisioned at enroll time by the
# control plane's --provision-auth (no host credential mounts needed).
FROM python:3.12-slim

# Job + installer dependencies. Add more here if your jobs need them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates procps gnupg \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only (talks to the host daemon via a mounted socket — DooD), so a
# worker can launch isolated per-job containers (e.g. GPU training jobs).
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
         > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install roost system-wide from the locally-built wheel. Version-agnostic glob so
# a pyproject version bump (e.g. 0.1.0 → 0.2.0, R32) doesn't break this COPY.
COPY dist/roost-*-py3-none-any.whl /tmp/
RUN pip install --no-cache-dir /tmp/roost-*-py3-none-any.whl && rm /tmp/*.whl

# Run as a non-root user: Claude Code refuses --dangerously-skip-permissions as
# root, and non-root is better hygiene for job isolation anyway.
RUN useradd --create-home --uid 1000 roost
USER roost
WORKDIR /home/roost
ENV PATH=/home/roost/.local/bin:$PATH

# Claude Code, native binary (no node/npm), arch matches the build host.
RUN curl -fsSL https://claude.ai/install.sh | bash

# Pre-create the mount targets as the roost user so Docker named volumes inherit
# uid-1000 ownership (otherwise they're created root-owned and config/cred writes
# fail with EACCES).
RUN mkdir -p /home/roost/.config/roost /home/roost/.claude

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/roost-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/roost-entrypoint.sh"]
