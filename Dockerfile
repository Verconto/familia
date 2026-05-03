# Base image pinned by digest. Tags like ``python3.12-bookworm-slim``
# are mutable — Astral re-publishes them on every Python patch / uv
# bump, so a future ``docker compose build`` could silently land us on
# a different bytes-for-bytes Python interpreter and break a transitive
# dep that's only known good against the version we tested. Bumping
# this digest is a deliberate release-time act, not a side-effect of
# ``--pull``.
#
# Refresh: ``docker pull ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# && docker inspect --format '{{index .RepoDigests 0}}' …``
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

# Build-time mirror fallbacks. Defaults point at canonical upstreams;
# operators on restricted networks (e.g. some RU egress paths where
# deb.debian.org / pypi.org / registry.npmjs.org are throttled) can
# pass ``--build-arg APT_MIRROR=https://mirror.yandex.ru/debian`` etc.
# Empty values keep the upstream default.
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""
ARG NPM_REGISTRY=""

# Apply APT_MIRROR if set. ``sources.list`` on bookworm-slim points at
# deb.debian.org/security.debian.org; we substitute the host portion.
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g; s|http://security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g; s|http://security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git bubblewrap openssh-client ffmpeg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Surface mirror fallbacks to pip/uv and npm for the rest of the build.
ENV PIP_INDEX_URL=${PIP_INDEX_URL:+$PIP_INDEX_URL}
ENV UV_INDEX_URL=${PIP_INDEX_URL:+$PIP_INDEX_URL}
ENV NPM_CONFIG_REGISTRY=${NPM_REGISTRY:+$NPM_REGISTRY}

WORKDIR /app

# Install Python deps first (cached layer). Copy only metadata files +
# the optional lock. When ``familia/requirements.lock`` is present
# (regenerated via ``bin/regen-lock.sh``), we resolve transitive deps
# from it — every wheel pinned to a specific version + hash, so a
# rebuild months from now installs bit-identical bytes. Without the
# lock we fall back to range-resolving from the pyprojects, which
# still respects the upper bounds we set there but lets transitive
# deps drift.
COPY nanobot/pyproject.toml nanobot/README.md nanobot/LICENSE /app/nanobot/
COPY familia/pyproject.toml familia/README.md /app/familia/
# COPY with a glob means "copy if exists, no-op otherwise" — but
# Dockerfile globs require at least one match. Workaround: COPY a known-
# present file (familia/README.md again) along with the optional lock,
# so the layer hash still depends on lock-file content when it exists.
COPY familia/README.md familia/requirements.loc[k] /app/familia/
RUN mkdir -p /app/nanobot/nanobot /app/nanobot/bridge /app/familia/src/familia && \
    touch /app/nanobot/nanobot/__init__.py /app/familia/src/familia/__init__.py && \
    if [ -f /app/familia/requirements.lock ]; then \
        echo "+ installing from requirements.lock (reproducible)"; \
        uv pip install --system --no-cache --require-hashes \
            -r /app/familia/requirements.lock && \
        uv pip install --system --no-cache --no-deps \
            /app/nanobot /app/familia; \
    else \
        echo "+ no lock file, resolving from pyproject ranges"; \
        uv pip install --system --no-cache /app/nanobot /app/familia; \
    fi && \
    rm -rf /app/nanobot/nanobot /app/nanobot/bridge /app/familia/src

# Copy full sources and reinstall the two editable packages without
# touching their (already-installed) dependency tree.
COPY nanobot/nanobot/ /app/nanobot/nanobot/
COPY nanobot/bridge/  /app/nanobot/bridge/
COPY familia/src/     /app/familia/src/
RUN uv pip install --system --no-cache --no-deps /app/nanobot /app/familia

# Build the WhatsApp bridge
WORKDIR /app/nanobot/bridge
RUN git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/ && \
    git config --global --add url."https://github.com/".insteadOf git@github.com: && \
    npm install && npm run build
WORKDIR /app

# Create non-root user and config directory.
#
# uid/gid are build-args so the container's ``nanobot`` matches the
# host operator running ``docker compose build``. Without this, the
# bind-mounted ``~/.nanobot`` ends up owned by the container's uid
# (default 1000) on the host, which on a multi-user VM (e.g. one
# where uid 1000 belongs to a different account than the operator)
# blocks the operator from writing to their own home dir.
# bootstrap.sh passes ``$(id -u)`` / ``$(id -g)`` of the operator
# (clamped to >= 1000 for security: never make the container user
# effectively root).
ARG NANOBOT_UID=1000
ARG NANOBOT_GID=1000
RUN groupadd -g ${NANOBOT_GID} nanobot 2>/dev/null \
        || groupmod -n nanobot $(getent group ${NANOBOT_GID} | cut -d: -f1) && \
    useradd -m -u ${NANOBOT_UID} -g ${NANOBOT_GID} -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    chown -R nanobot:nanobot /home/nanobot /app

COPY nanobot/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

USER nanobot
ENV HOME=/home/nanobot

EXPOSE 18790

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
