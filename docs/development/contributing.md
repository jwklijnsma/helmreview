# Contributing

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use a mock Ollama server (`tests/mock_ollama.py`), so they need no GPU.
Tests that render charts need `helm` on the `PATH` and are skipped without it.

## Web UI

The React app is in `web/` (Vite, TypeScript). `src/helmreview/web/static/` is
its build output and is committed, so after changing `web/src/` run `make web`
and commit both. CI rebuilds it and fails when they differ (for Renovate and
Dependabot PRs it pushes the rebuilt bundle itself).

```bash
make serve        # API on :8766 (in one terminal)
make web-dev      # http://localhost:5173 with hot reload, /api proxied to :8766
make web-test     # typecheck + vitest
make web          # production build into src/helmreview/web/static/
```

## Documentation

This site is built with [Zensical](https://zensical.org) from `docs/` and
`zensical.toml`.

```bash
pip install -r requirements-docs.txt
zensical serve            # preview on http://localhost:8000
zensical build --strict   # what CI runs
```

The `Docs` workflow builds the site on every pull request that touches the docs
and fails on links to missing pages or anchors. Pushes to `main` publish it to
GitHub Pages.

## Layout

| Path | |
|---|---|
| `src/helmreview/cli.py` | Commands and flags |
| `src/helmreview/config.py` | Settings, defaults, environment variables |
| `src/helmreview/discover.py` | Finding charts and manifest folders |
| `src/helmreview/lint.py` | `helm lint` and `helm template` |
| `src/helmreview/static.py` | The HR checks |
| `src/helmreview/review.py`, `ollama.py`, `cache.py`, `filequeue.py` | Model review, Ollama client, cache, queue |
| `src/helmreview/fix.py` | `fix` |
| `src/helmreview/report.py` | Report formats |
| `src/helmreview/gitlab.py` | `watch-mrs` |
| `src/helmreview/perf.py` | `doctor` speed check |
| `src/helmreview/web/` | `serve` and the built UI |
| `web/` | UI source |
| `deploy/systemd/` | Service units and `install.sh` |
| `examples/` | GitLab CI and a sample chart repo |
| `scripts/` | Ollama server tuning |
| `docs/`, `zensical.toml` | This site |
