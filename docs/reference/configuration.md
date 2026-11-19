# Configuration

`helmreview init` writes a sample `.helmreview.yml`. Settings are read in this
order, later wins:

1. built-in defaults
2. `~/.helmreview.yml`
3. the repo's `.helmreview.yml` (or `.helmreview.yaml`)
4. an extra file given with `-c`
5. environment variables
6. CLI flags

Unknown keys are reported as a warning and ignored.

## Example

```yaml
model: qwen2.5-coder:14b        # any Ollama model; qwen3-coder:30b if VRAM allows
language: en                    # en | nl
num_ctx: 16384                  # context window sent to Ollama
workers: 1                      # parallel requests; raise if OLLAMA_NUM_PARALLEL > 1
fail_on: high                   # exit 1 when a finding is at/above: info|low|medium|high|critical
min_severity: info              # hide findings below this level in reports
min_confidence: 0.5             # drop model findings it is unsure about
lint_strict: false              # helm lint --strict
disabled_rules: []              # e.g. [HR009, helm-lint:icon]
exclude:
  - ".git/**"
  - "**/charts/*.tgz"

house_rules: |
  - Every value in values.yaml has a comment explaining it.
  - Images come from our registry (registry.example.com), never docker.io directly.
  - Charts must install on OpenShift with the restricted-v2 SCC.
```

## Model and Ollama

| Key | Default | Notes |
|---|---|---|
| `model` | `qwen2.5-coder:14b` | env `HELMREVIEW_MODEL`, `-m` |
| `ollama_host` | `http://localhost:11434` | env `OLLAMA_HOST`, `--host`; **ignored in a repo config** |
| `api_key` | – | Bearer token if Ollama sits behind an auth proxy; ignored in a repo config |
| `num_ctx` | `16384` | Context window per request |
| `workers` | `1` | Parallel requests; keep it at or below the server's `OLLAMA_NUM_PARALLEL` |
| `timeout` | `600` | Max seconds without model output; env `HELMREVIEW_TIMEOUT` |
| `deadline` | `0` | Max seconds per request (`0` = 2 × `timeout`) |
| `num_predict` | `4096` | Cap on generated tokens; stops a model stuck repeating JSON |
| `keep_alive` | `30m` | Keeps the model loaded between requests |
| `temperature` | `0.1` | |
| `retries` | `2` | |

## Scan

| Key | Default | Notes |
|---|---|---|
| `language` | `en` | Report language: `en` or `nl` |
| `per_file` | `true` | One model request per file; `false` groups a chart's files |
| `values_context_chars` | `6000` | How much of `values.yaml` goes along with a template |
| `max_chars_per_unit` | `24000` | Larger charts are split into several requests |
| `max_file_chars` | `20000` | Single huge files are truncated |
| `fail_on` | `high` | Exit `1` at or above: `info` `low` `medium` `high` `critical` |
| `min_severity` | `info` | Hide findings below this |
| `min_confidence` | `0.5` | Drop model findings the model is unsure about |
| `lint_strict` | `false` | `helm lint --strict` (`--strict`) |
| `disabled_rules` | `[]` | HR rule IDs or `helm-lint:icon`, `helm-lint:warning`, … |
| `exclude` | sensible list | Glob patterns: `.git`, venvs, `node_modules`, `.helmreview`, packaged `charts/*.tgz`, … |
| `house_rules` | – | Free text the model enforces, e.g. "images only from our registry" |

## Fix

| Key | Default | Notes |
|---|---|---|
| `fix_min_severity` / `fix_min_confidence` | `medium` / `0.8` | Which model findings `fix` may touch |
| `fix_batch_size` | `0` | Findings per request; `0` = all of a file at once (`--batch-size`) |
| `fix_context_lines` / `fix_full_file_lines` | `25` / `120` | Excerpt size for long files |

## GitLab merge requests

Only honoured from `~/.helmreview.yml`, the environment and flags, never from a
repo config. See [GitLab merge requests](../usage/merge-requests.md#settings).

| Key | Default |
|---|---|
| `gitlab_url` | – |
| `gitlab_projects` / `gitlab_groups` | `[]` |
| `gitlab_token_env` | `GITLAB_TOKEN` |
| `gitlab_interval` | `120` |
| `gitlab_skip_drafts` / `gitlab_skip_forks` | `false` |
| `gitlab_max_age_days` | `14` |

## Environment variables

| Variable | Key |
|---|---|
| `OLLAMA_HOST`, `HELMREVIEW_OLLAMA_HOST` | `ollama_host` |
| `HELMREVIEW_MODEL` | `model` |
| `HELMREVIEW_API_KEY` | `api_key` |
| `HELMREVIEW_LANGUAGE` | `language` |
| `HELMREVIEW_WORKERS` | `workers` |
| `HELMREVIEW_NUM_CTX` | `num_ctx` |
| `HELMREVIEW_TIMEOUT` | `timeout` |
| `HELMREVIEW_NUM_PREDICT` | `num_predict` |
| `HELMREVIEW_FIX_BATCH_SIZE` | `fix_batch_size` |
| `HELMREVIEW_FAIL_ON` | `fail_on` |
| `HELMREVIEW_GITLAB_URL` | `gitlab_url` |
| `HELMREVIEW_GITLAB_TOKEN_ENV` | `gitlab_token_env` |
| `HELMREVIEW_GITLAB_PROJECTS` / `HELMREVIEW_GITLAB_GROUPS` | comma-separated |
| `HELMREVIEW_GITLAB_INTERVAL` | `gitlab_interval` |
| `HELMREVIEW_GITLAB_SKIP_DRAFTS` / `HELMREVIEW_GITLAB_SKIP_FORKS` | |

Other variables:

| Variable | |
|---|---|
| `HELMREVIEW_TRUST_REPO_CONFIG=1` | Let a repo config set `ollama_host`, `api_key` and the `gitlab_*` keys |
| `HELMREVIEW_WEB_TOKEN` | Access token for the [web UI](../usage/web-ui.md) |
| `HELMREVIEW_WATCH_MRS=1` | `serve` also reviews merge requests (container) |
| `GITLAB_TOKEN` | GitLab token, or the variable named in `gitlab_token_env` |
| `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO` | Internal CA for GitLab |

## Repo configs are not trusted

A repo config cannot change `ollama_host`, `api_key` or the `gitlab_*` keys
unless you set `HELMREVIEW_TRUST_REPO_CONFIG=1`. That way, a repo you cloned
cannot send your code to someone else's server.
