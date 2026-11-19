# Scan

`scan` is the default command: `helmreview` and `helmreview scan` do the same.

```bash
helmreview                                   # scan the current directory
helmreview ~/git/platform-charts             # scan another repo
helmreview --host http://gpu-box:11434       # use a remote Ollama server
helmreview --model qwen3-coder:30b --lang nl # other model, Dutch report
helmreview --diff origin/main                # only review what changed (for MRs)
helmreview --only charts/keycloak            # one chart
helmreview --no-llm                          # just helm lint + HR checks (fast)
helmreview -f md,json,sarif,gitlab -o out/   # pick report formats and folder
```

## What it reviews

- **Charts:** every folder with a `Chart.yaml`, subcharts included.
- **Plain manifests:** every folder of Kubernetes YAML outside the charts:
  Kustomize bases, Argo CD apps, raw YAML.

## What happens

1. **`helm lint`** runs on every chart (`--strict` or `lint_strict: true` for
   `helm lint --strict`).
2. Every chart is **rendered with `helm template`** and its default values.
   The **HR checks** run on each rendered object, and each finding is mapped
   back to the template it came from (the line is the first match for the key in
   that template). Plain manifests get the same object checks. See
   [Built-in rules](../reference/rules.md).
3. The **model** gets one request per file (or per chart with
   `per_file: false`), with up to `values_context_chars` of the chart's
   `values.yaml` next to each template, and with what the first two layers
   already found, so it looks for new problems.
4. Findings below `min_severity`, model findings below `min_confidence`, and
   rules in `disabled_rules` are dropped.
5. The reports are written to `<repo>/.helmreview/`. See [Reports](../reference/reports.md).

Each layer can be switched off: `--no-lint`, `--no-static`, `--no-llm`. When
Ollama can't be reached, the scan carries on without the model and says so;
`--require-llm` makes that an error instead.

!!! note "Dependencies that aren't vendored"
    Charts whose dependencies are not in `charts/` (no `helm dependency build`)
    are rendered without them, so the chart's own templates are still checked.
    The report says so.

## Diff mode

```bash
helmreview --diff origin/main
```

`--diff <ref>` reviews only the charts that contain changed files. The model
still sees the whole file for context, but only findings on **added or modified
lines** are reported. Use `--diff-scope files` to report everything in the
changed files.

This is what the [merge-request reviewer](merge-requests.md) and the
[CI job](../deploy/ci.md) use. In CI, fetch enough history for the ref to
exist (`GIT_DEPTH: "0"` in GitLab).

## Limit the scan

| Flag | Effect |
|---|---|
| `--only charts/web` | Only charts or paths that match (repeatable) |
| `--exclude 'charts/legacy/**'` | Skip paths (repeatable; adds to `exclude`) |
| `--disable HR009` | Drop a rule (repeatable; adds to `disabled_rules`) |
| `--min-severity medium` | Drop findings below this severity |
| `--min-confidence 0.7` | Drop model findings the model is less sure about |

## Exit code

| Code | Meaning |
|---|---|
| `0` | No finding at or above `--fail-on` |
| `1` | At least one finding at or above `--fail-on` (default `high`) |
| `2` | Tool error (bad config, `--require-llm` without Ollama, …) |

So a scan works as a pipeline gate.

## Cache

Model answers are cached in `.helmreview/scan-cache.json`. A re-run only sends
files that changed. Requests that timed out are remembered too, and are split
straight away next time. `--no-cache` asks the model again for everything.

## Watching a long run

The status of every file (pending, running, done, cached, failed) is kept in
`.helmreview/queue.json`. From another terminal:

```bash
jq '.counts' .helmreview/queue.json
```

All flags are listed under [Commands](../reference/cli.md#scan).
