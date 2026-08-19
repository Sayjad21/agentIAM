# AgentIAM — T-056. One image, three entrypoints (ADR-056 §5.1): the control plane, the
# PEP, and the one-shot migration job all run from this image, selected by the command
# `docker-compose.demo.yml` or the k3s manifests give the container — not by three
# separately-built images that would triple the signing and scanning surface for
# identical layers.
#
# **Editable install, on purpose — not `uv sync --no-editable`.** Measured before
# deciding: `packages/agentiam-controlplane/alembic.ini`'s `script_location` is a path
# relative to alembic's *working directory*, not to the ini file
# (`alembic -c .../alembic.ini current` from an unrelated directory fails with
# `Path doesn't exist: src/agentiam_controlplane/db/migrations`). The migration job
# therefore runs with the package directory as its working directory, and that only
# works if `src/` is actually present in the image at runtime — which a `--no-editable`
# install, correct as it is for most projects, would not guarantee. Keeping the default
# editable workspace install means the container's file layout matches local dev exactly,
# so every path assumption already proven by the test suite holds here too.

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM python:3.12-slim-bookworm AS base

# Belongs to every stage that runs Python: LANG/LC_ALL pinned the same way
# docker-compose.yml already pins Postgres's locale (money is NUMERIC(20,4); formatting
# and ordering must not depend on the host). PYTHONDONTWRITEBYTECODE keeps the image free
# of stray .pyc layers from the sync step.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layer first, cache-friendly: only pyproject.toml/uv.lock and every workspace
# member's pyproject.toml are needed to resolve and install dependencies, so this layer
# survives any change to application code.
COPY pyproject.toml uv.lock ./
COPY packages/agentiam-core/pyproject.toml packages/agentiam-core/
COPY packages/agentiam-sdk/pyproject.toml packages/agentiam-sdk/
COPY packages/agentiam-pep/pyproject.toml packages/agentiam-pep/
COPY packages/agentiam-controlplane/pyproject.toml packages/agentiam-controlplane/
COPY packages/agentiam-demo/pyproject.toml packages/agentiam-demo/

# `--no-install-workspace` skips the local packages here — there is no source yet, only
# their pyproject.toml — so this installs third-party dependencies only, which is the
# layer worth caching. The second `uv sync` below (after the real source lands) is then
# fast because every wheel is already present.
RUN uv sync --frozen --no-dev --no-install-workspace

COPY packages/ packages/
COPY scripts/ scripts/

RUN uv sync --frozen --no-dev

# A non-root user for every stage that actually runs the app. 1000 rather than `--system`
# (which picks a UID below Debian's SYS_UID_MAX=999 and warns when given 1000 explicitly,
# measured): 1000 is the conventional first regular UID and is what a bind-mounted host
# directory's ownership usually lines up with, which matters more here since nothing in
# this image runs as a system daemon. `/app` is owned by it so `uv` can still write
# bytecode caches at runtime without needing root.
RUN groupadd --gid 1000 agentiam \
    && useradd --uid 1000 --gid agentiam --home-dir /app --shell /usr/sbin/nologin agentiam \
    && chown -R agentiam:agentiam /app

# Pre-created and owned by `agentiam` so a fresh *named* Docker volume mounted here
# inherits this ownership on first use — Docker copies the image directory's existing
# content and ownership into an empty named volume the first time it is mounted.
# Measured: without this, `docker-compose.demo.yml`'s `demo-secrets` volume mounts as
# root:root and the bootstrap container (uid 1000) gets `PermissionError` writing to it.
RUN mkdir -p /secrets && chown agentiam:agentiam /secrets

USER agentiam

ENV PATH="/app/.venv/bin:${PATH}"

# No ENTRYPOINT/CMD: the three roles (control plane, PEP, migration) are three different
# commands, given explicitly by whatever orchestrates the container
# (`docker-compose.demo.yml`, the k3s manifests). A default CMD here would only be right
# for one of the three and silently wrong for the other two.
