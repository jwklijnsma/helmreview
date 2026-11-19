# Docker

Every push to `main` publishes `ghcr.io/jwklijnsma/helmreview` (amd64 + arm64)
with helm included. Ollama is not in the image; by default it talks to
Ollama on the Docker host. Without a command the container starts the
[web UI](../usage/web-ui.md) on port 8766 for the repositories under `/repos`.

## Web UI and one-off scans

```bash
# Web UI for every repo under ~/git
docker run -d --name helmreview -p 127.0.0.1:8766:8766 \
  --add-host=host.docker.internal:host-gateway \
  --user "$(id -u):$(id -g)" -v ~/git:/repos \
  -e HELMREVIEW_WEB_TOKEN="$(openssl rand -hex 24)" \
  ghcr.io/jwklijnsma/helmreview
# the link with the token: docker logs helmreview 2>&1 | grep open:

# One-off scan of the current directory (CI, pre-merge checks)
docker run --rm --add-host=host.docker.internal:host-gateway \
  --user "$(id -u):$(id -g)" -v "$PWD:/repos/work" \
  ghcr.io/jwklijnsma/helmreview scan /repos/work
```

- `--user "$(id -u):$(id -g)"` makes reports and fixes belong to you, not root.
  The image also runs under arbitrary UIDs (OpenShift).
- On Linux, `--add-host=host.docker.internal:host-gateway` is needed to reach
  Ollama on the host. Ollama must listen on more than 127.0.0.1
  (`OLLAMA_HOST=0.0.0.0` in its service). Or set
  `-e OLLAMA_HOST=http://gpu-box:11434`.
- Pass settings with `HELMREVIEW_MODEL`, `HELMREVIEW_NUM_CTX` and so on (see
  [environment variables](../reference/configuration.md#environment-variables)),
  or mount a config:
  `-v ~/.helmreview.yml:/home/helmreview/.helmreview.yml:ro`.

## Tags

| Tag | Follows |
|---|---|
| `latest`, `main` | The main branch |
| `0.1.0`, `0.1` | Releases (see [Releasing](../development/releasing.md)) |
| `sha-abc1234` | One exact build |

## Docker Compose with Ollama on the GPU

```yaml
services:
  ollama:
    image: ollama/ollama
    volumes: [ollama:/root/.ollama]
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: all, capabilities: [gpu] }]
  helmreview:
    image: ghcr.io/jwklijnsma/helmreview
    user: "1000:1000"            # your uid:gid
    ports: ["127.0.0.1:8766:8766"]
    environment:
      OLLAMA_HOST: http://ollama:11434
      HELMREVIEW_WEB_TOKEN: ${HELMREVIEW_WEB_TOKEN:?put HELMREVIEW_WEB_TOKEN=... in .env}
    volumes: [~/git:/repos]
    depends_on: [ollama]
volumes:
  ollama:
```

Pull the model once:

```bash
docker compose exec ollama ollama pull qwen2.5-coder:14b
```

To run the container as a service that starts at boot, see
[systemd service](systemd.md).
