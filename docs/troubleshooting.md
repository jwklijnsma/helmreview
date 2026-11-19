# Troubleshooting

Start with `helmreview doctor` (add `--host` for a remote Ollama). It checks
helm, Ollama, the model and its speed.

## Symptoms

| Symptom | Likely cause | Fix |
|---|---|---|
| `HR000` on a chart | The chart does not render with its default values, or has a YAML error | Run `helm template <chart>` yourself; required values without a default are a common cause |
| The report says dependencies were not rendered | `charts/` has no vendored dependencies | `helm dependency build <chart>`; the chart's own templates are checked either way |
| `helm` not found | helm is not on the `PATH` | Install helm 3 or 4, or use the container image |
| `warning: Ollama not reachable … LLM review skipped` | Wrong host, Ollama only listens on 127.0.0.1, or a firewall | Check `--host` / `OLLAMA_HOST`. On the Ollama host set `OLLAMA_HOST=0.0.0.0` in its service. Use `--require-llm` to make this an error |
| Container can't reach Ollama on the host | `host.docker.internal` is not defined on Linux | Add `--add-host=host.docker.internal:host-gateway`, or set `OLLAMA_HOST` to the host's address |
| Every file takes minutes | Part of the model runs on the CPU, or the model reloads between requests | See [Speed and tuning](speed.md) |
| Requests time out | Model too big for the GPU, or too many `workers` for the server's parallel slots | Lower `workers`, use a smaller model, or raise `timeout` |
| `ollama_host` in the repo's `.helmreview.yml` is ignored | On purpose: a cloned repo must not send your code to another server | Put it in `~/.helmreview.yml`, the environment or `--host`; or set `HELMREVIEW_TRUST_REPO_CONFIG=1` |
| `fix` refuses to run | The repo has uncommitted changes | Commit or stash them, or pass `--allow-dirty` |
| A fix was not applied | The edit did not match exactly once, broke the YAML, or the chart no longer rendered | See `.helmreview/fix-report.md`; it says why |
| Too many low findings | Rules that don't matter for your charts | `disabled_rules: [HR009, HR019]` or `--min-severity medium` |
| Findings on helm test hooks | – | Test hooks are not held to the hardening, resource and probe rules |
| The web UI says the token is wrong | The token changed at a restart (none was set) | Set `--token` or `$HELMREVIEW_WEB_TOKEN`, or read the new link from the log |
| The service doesn't start | The token in the settings file is still the example value | Set a real token, or run `install.sh --new-token`; see [systemd service](deploy/systemd.md) |
| A merge request gets no comment | It changes nothing helmreview reviews, is a draft or fork you skip, or is older than `gitlab_max_age_days` | `helmreview watch-mrs … --dry-run` lists what would be reviewed |
| GitLab calls fail with certificate errors | Internal CA | Set `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` to your CA bundle |

## Tips

- **Large repos.** Start with `--no-llm` to clear helm lint and HR findings
  cheaply, then run the model.
- **Only a branch's changes.** `--diff origin/main` reviews only the changed
  lines; see [diff mode](usage/scan.md#diff-mode).
- **House rules.** Tell the model what your platform expects, e.g. "images only
  from our registry" or "must install with the restricted-v2 SCC", in
  `house_rules`.
- **Ask the model again.** Answers are cached; `--no-cache` ignores the cache.
- **Where are the logs?** CLI runs print to the terminal. Services log to the
  journal: `journalctl [--user] -u helmreview -f`.
