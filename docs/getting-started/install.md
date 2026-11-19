# Install

## Requirements

| What | Notes |
|---|---|
| Python 3.10+ | |
| helm 3 or 4 | On the `PATH`: <https://helm.sh/docs/intro/install/>. Used for `helm lint` and `helm template` only; it never talks to a cluster |
| [Ollama](https://ollama.com) | On this machine or another one with a GPU. Not needed for `--no-llm` |
| A coding model | `qwen2.5-coder:14b` by default; see [model choice](../speed.md#model-choice) |
| git | For [diff mode](../usage/scan.md#diff-mode), [fix](../usage/fix.md) and [merge requests](../usage/merge-requests.md) |

## With pip

```bash
git clone https://github.com/jwklijnsma/helmreview.git && cd helmreview
pip install .                 # Python 3.10+
ollama pull qwen2.5-coder:14b # any coding model works; bigger is better if VRAM allows
helmreview doctor             # checks helm, Ollama and the model
```

`helmreview doctor` checks that helm is found, that Ollama answers, that the
model is there, and measures its speed. See
[Speed and tuning](../speed.md#1-run-helmreview-doctor) for what it reports.

## Container

The image `ghcr.io/jwklijnsma/helmreview` has helm built in and needs no
Python on the host. See [Docker](../deploy/docker.md), or
[systemd service](../deploy/systemd.md) to install it as a service in one
command.

## Next

- [Quick start](quickstart.md): your first scan.
- [Configuration](../reference/configuration.md): point it at a remote Ollama
  server, change the model or the report language.
