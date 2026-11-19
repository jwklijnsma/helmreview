# Commands

```text
helmreview [scan] [path]    review the codebase (default command)
helmreview fix [path]       fix findings with the model (verified by helm template)
helmreview doctor [path]    check helm, Ollama and the model
helmreview rules            list built-in rules
helmreview init [path]      write a sample .helmreview.yml (--force to overwrite)
helmreview serve            start the web UI
helmreview watch-mrs        review GitLab merge requests as they come in
helmreview --version
```

`path` is the repository root and defaults to the current directory.

## Common options

These work on `scan`, `fix`, `doctor` and `watch-mrs`:

| Option | |
|---|---|
| `-c, --config FILE` | Extra config file |
| `-m, --model MODEL` | Ollama model (default `qwen2.5-coder:14b`) |
| `--host URL` | Ollama URL, e.g. `http://gpu-box:11434` |

## scan

| Option | |
|---|---|
| `--diff BASE` | Only review charts changed vs a git ref, e.g. `origin/main` |
| `--diff-scope {lines,files}` | With `--diff`: report only on changed lines (default) or whole changed files |
| `--only PATTERN` | Limit to charts/paths that match (repeatable), e.g. `charts/web` |
| `-f, --format FORMAT` | `md,json,sarif,gitlab` (default `md,json`) |
| `-o, --output DIR` | Report directory (default `<repo>/.helmreview`) |
| `--lang {en,nl}` | Report language |
| `--fail-on LEVEL` | Exit `1` at or above this severity |
| `--min-severity LEVEL` | Hide findings below this severity |
| `--min-confidence N` | Drop model findings below this confidence |
| `--workers N` | Parallel model requests |
| `--num-ctx N` | Context window |
| `--timeout S` | Max seconds without model output (default 600) |
| `--no-cache` | Ignore `.helmreview/scan-cache.json` and ask the model again |
| `--strict` | `helm lint --strict` |
| `--exclude GLOB` | Skip paths (repeatable) |
| `--disable RULE` | Drop a rule (repeatable) |
| `--no-llm` / `--no-lint` / `--no-static` | Skip the model / helm lint / the HR checks |
| `--require-llm` | Fail if Ollama is unreachable |
| `-q, --quiet` | |

## fix

| Option | |
|---|---|
| `--from-report FILE` | `report.json` to fix from (default `<repo>/.helmreview/report.json`; a scan runs first when it does not exist) |
| `--dry-run` | Only write `fix.patch`, change nothing |
| `--allow-dirty` | Run even with uncommitted changes in the repo |
| `--only PATH` | Limit to paths (repeatable) |
| `--min-severity LEVEL` | Model fixes only for findings at or above this (default `medium`) |
| `--min-confidence N` | Model fixes only at or above this confidence (default `0.8`) |
| `--no-llm` | Skip model fixes |
| `--no-static-fix` | Do not list the HR findings that need a human |
| `--lang {en,nl}` | |
| `--workers N` | Parallel model requests; match `OLLAMA_NUM_PARALLEL` on the server |
| `--timeout S` | Max seconds without model output (default 600) |
| `--batch-size N` | Findings per model request (config `fix_batch_size`; split further on timeout) |
| `--no-cache` | Ignore `.helmreview/fix-cache.json` |
| `-o, --output DIR` | Where reports live (default `<repo>/.helmreview`) |
| `-q, --quiet` | |

## doctor

| Option | |
|---|---|
| `--no-speed` | Skip the speed check |

## serve

| Option | |
|---|---|
| `--root DIR` | A repo, or a folder of repos, the UI may scan (repeatable; default `.`) |
| `--bind ADDR` | Address to listen on (default `127.0.0.1`; `0.0.0.0` for all) |
| `--port N` | Port (default `8766`) |
| `--token TOKEN` | Access token (default `$HELMREVIEW_WEB_TOKEN` or a random one) |
| `--no-auth` | No access token (only allowed with a localhost `--bind`) |
| `--open` | Open the UI in a browser |
| `-v, --verbose` | Log every request |
| `--upload-dir DIR` | Where uploaded ZIPs are unpacked (default `~/.cache/helmreview/uploads`) |
| `--max-upload-mb MB` | Largest ZIP accepted (default 100) |
| `--upload-keep-days DAYS` | Delete uploads older than this (default 7, `0` = keep) |
| `--no-upload` | Switch ZIP uploads off |
| `--watch-mrs` | Also review GitLab merge requests in the background (same as `$HELMREVIEW_WATCH_MRS=1`) |

## watch-mrs

| Option | |
|---|---|
| `--gitlab-url URL` | e.g. `https://gitlab.example.nl` |
| `--project PATH` | Project to watch, `group/name` or id (repeatable) |
| `--group PATH` | Watch every project in this group and its subgroups (repeatable) |
| `--interval SECONDS` | Seconds between polls (default 120) |
| `--once` | Poll once and exit (cron, CI) |
| `--dry-run` | Only list the merge requests that would be reviewed |
| `--skip-drafts` / `--skip-forks` | Leave draft / fork merge requests alone |
| `--state-dir DIR` | Checkouts and what was reviewed (default `~/.cache/helmreview/mr`) |
| `--lang {en,nl}` | Language of the comment |
| `--no-llm` | Skip the Ollama review |

## Exit codes

| Code | |
|---|---|
| `0` | OK |
| `1` | A finding at or above `--fail-on` |
| `2` | Tool error |
