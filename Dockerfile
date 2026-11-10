# syntax=docker/dockerfile:1
#
# helmreview: helm lint + HR checks on the rendered charts + a local Ollama model, with the web UI.
#
#   docker run -d -p 127.0.0.1:8766:8766 \
#     --add-host=host.docker.internal:host-gateway \
#     --user "$(id -u):$(id -g)" -v ~/git:/repos \
#     -e HELMREVIEW_WEB_TOKEN="$(openssl rand -hex 24)" ghcr.io/jwklijnsma/helmreview
#
#   As a systemd service (Podman Quadlet or Docker): see deploy/systemd/.
#
#   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repos/work" \
#     ghcr.io/jwklijnsma/helmreview scan /repos/work
#
# Ollama is not in this image; point OLLAMA_HOST at it (default: the Docker host).

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- helm
# The official release binary, checked against its published sha256.
FROM python:${PYTHON_VERSION}-slim AS helm
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=4.3.1
ARG TARGETARCH
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/helm
RUN set -eux; arch="${TARGETARCH:-amd64}"; \
    tgz="helm-v${HELM_VERSION}-linux-${arch}.tar.gz"; \
    curl -fsSLO "https://get.helm.sh/${tgz}"; \
    curl -fsSLo sum "https://get.helm.sh/${tgz}.sha256sum"; \
    echo "$(cut -d' ' -f1 sum)  ${tgz}" | sha256sum -c -; \
    tar -xzf "${tgz}" "linux-${arch}/helm"; \
    install -m 0755 "linux-${arch}/helm" /usr/local/bin/helm; \
    helm version --short

# ---------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# DL3013: this installs the local project; its dependency ranges live in pyproject.toml.
# hadolint ignore=DL3013
RUN pip install . \
 && helmreview --version

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="helmreview" \
      org.opencontainers.image.description="Review Helm charts with helm lint, static security checks and a local Ollama model" \
      org.opencontainers.image.source="https://github.com/jwklijnsma/helmreview" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# - git: diff mode, the dirty-tree guard and `fix`; tini: a proper PID 1 (signals, zombies).
# - safe.directory: mounted repos belong to the host user, not to whoever runs in the
#   container, and git would refuse them as "dubious ownership".
# - $HOME is writable for any UID (docker --user, OpenShift's random UIDs), because
#   helm keeps its cache there. ~/.cache/helmreview holds the ZIP uploads; mount a
#   volume there to keep them (a new volume copies this folder's permissions).
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*' \
 && useradd --uid 1000 --gid 0 --home-dir /home/helmreview --create-home --shell /usr/sbin/nologin helmreview \
 && chmod 1777 /home/helmreview \
 && mkdir -p /home/helmreview/.cache/helmreview \
 && chmod 1777 /home/helmreview/.cache /home/helmreview/.cache/helmreview \
 && mkdir -p /repos \
 && chmod 0775 /repos && chgrp 0 /repos

COPY --from=build /opt/venv /opt/venv
COPY --from=helm /usr/local/bin/helm /usr/local/bin/helm

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/helmreview \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HELMREVIEW_CONTAINER=1 \
    OLLAMA_HOST=http://host.docker.internal:11434

USER 1000:0
WORKDIR /repos
EXPOSE 8766

# Only meaningful for `serve`; one-shot `scan`/`fix` runs exit before the first check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8766/', timeout=4); sys.exit(0)"]

# SIGINT lets `serve` cancel running jobs and shut down cleanly on `docker stop`.
STOPSIGNAL SIGINT
ENTRYPOINT ["tini", "--", "helmreview"]
CMD ["serve", "--root", "/repos", "--bind", "0.0.0.0", "--port", "8766"]
