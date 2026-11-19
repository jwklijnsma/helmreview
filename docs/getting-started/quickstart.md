# Quick start

## 1. Check the setup

```bash
helmreview doctor --host http://gpu-box:11434
```

Leave out `--host` when Ollama runs on the same machine.

## 2. Scan a repository

```bash
cd ~/git/platform-charts
helmreview --host http://gpu-box:11434
```

It finds every chart (a folder with `Chart.yaml`, subcharts included) and every
folder of plain Kubernetes YAML outside the charts. Every file is reviewed in
turn, with progress like
`[12/40] ✓ charts/web/templates/deployment.yaml 41s`. The reports land in
`.helmreview/` in the repo. Add that folder to `.gitignore`.

!!! tip "Start fast"
    On a big repo, run `helmreview --no-llm` first. It runs only `helm lint` and
    the HR checks and takes seconds. Clear those findings, then run the model.

## 3. Read the report

Open `.helmreview/report.md`, or look at the summary in the terminal. Every
finding has a severity, a rule, a file and line, and a suggested fix. Findings
on rendered objects point at the template they came from. Model findings start
with `llm:` and carry a confidence score.

## 4. Fix what it found

```bash
helmreview fix --dry-run    # writes .helmreview/fix.patch, changes nothing
helmreview fix              # applies the fixes to your working tree
git diff                    # review; undo with git checkout -- .
```

Nothing is committed, and every edit is checked with `helm template`. See
[Automatic fixes](../usage/fix.md).

## 5. Save your settings

```bash
helmreview init             # writes a sample .helmreview.yml
```

Put settings that are about you (Ollama host, model) in `~/.helmreview.yml`,
and settings that are about the repo (excluded paths, house rules) in the
repo's `.helmreview.yml`. See [Configuration](../reference/configuration.md).

## Where next

- Only review what a branch changed: [diff mode](../usage/scan.md#diff-mode).
- Work from the browser: [Web UI](../usage/web-ui.md).
- Review merge requests automatically: [GitLab merge requests](../usage/merge-requests.md).
