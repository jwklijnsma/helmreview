# helmreview

Review Helm charts and Kubernetes manifests with three layers, all running on your own hardware:

1. **`helm lint`** for chart structure, template errors and deprecated APIs.
2. **Built-in static checks (HR0xx) on what the chart actually produces.** Every chart is
   rendered with `helm template` and its default values, and each object is checked and
   mapped back to the template it came from: privileged or root containers, missing
   securityContext hardening (the *restricted* Pod Security Standard / OpenShift
   `restricted-v2`), host namespaces and hostPath, `latest` image tags, missing resources and
   probes, cluster-admin and wildcard RBAC, removed API versions, Ingress/Route without TLS,
   hard-coded namespaces, hard-coded secrets in `values.yaml`, unpinned dependencies. Plain
   manifests (Kustomize bases, Argo CD apps, raw YAML) get the same object checks.
3. **A local Ollama model** that reviews each template with the chart's `values.yaml` next to
   it: wrong scope (`.` vs `$`), values that don't exist, selectors that don't match the pods,
   names that can exceed 63 characters, missing checksum annotations, upgrade traps. It is told
   what the first two layers found, so it looks for new problems.

No code leaves your network, and `helm` never talks to a cluster. Reports come out as
Markdown, JSON, SARIF and GitLab Code Quality.

`helmreview fix` lets the model fix its findings, and keeps only edits after which the chart
still renders.

## Install

```bash
pip install .                 # Python 3.10+
# helm 3 or 4 on the PATH: https://helm.sh/docs/intro/install/
ollama pull qwen2.5-coder:14b # any coding model works; bigger is better if VRAM allows
helmreview doctor             # checks helm, Ollama and the model
```

Or use the container image, which has helm built in (see [Docker](#docker)).

## Use

```bash
helmreview                                   # scan the current directory
helmreview ~/git/platform-charts             # scan another repo
helmreview --host http://gpu-box:11434       # use a remote Ollama server
helmreview --model qwen3-coder:30b --lang nl # other model, Dutch report
helmreview --diff origin/main                # only review what changed (for MRs)
helmreview --only charts/keycloak            # one chart
helmreview --no-llm                          # just helm lint + HR checks (fast)
helmreview serve --root ~/git                # web UI (see below)
helmreview -f md,json,sarif,gitlab -o out/   # pick report formats and folder
```

It finds every chart (a folder with `Chart.yaml`, subcharts included) and every folder of
plain Kubernetes YAML outside the charts. Reports go to `<repo>/.helmreview/` by default (add
it to `.gitignore`). The exit code is `1` when a finding is at or above `--fail-on` (default
`high`), `2` on a tool error, and `0` otherwise, so it works as a pipeline gate.

Charts whose dependencies are not vendored in `charts/` (no `helm dependency build`) are
rendered without them, so the chart's own templates are still checked; the report says so.

### Diff mode

`--diff <ref>` reviews only the charts that contain changed files. The model still sees the
whole file for context, but only findings on **added or modified lines** are reported (use
`--diff-scope files` to report everything in the changed files).

## Automatic fixes

```bash
helmreview fix                   # fix what the last scan found (runs a scan first if needed)
helmreview fix --dry-run         # only write .helmreview/fix.patch
helmreview fix --only charts/web # one chart
```

The model gets each file with its findings, verifies them (many model findings are false
positives) and either declines them or returns small exact search/replace edits. Every edit is
checked before anything is written:

- it must match the file exactly once and stay small (a model that rewrites the file is refused);
- `values.yaml`, `Chart.yaml` and manifests must still parse as YAML;
- **the chart must still render** with `helm template`: if it rendered before and an edit
  breaks it, the file helm names is reverted (and, if that is not enough, the chart's other
  edits too).

The HR findings are listed in the fix report as *needs a human*, with what to do: they touch
values and defaults that are yours to decide. Only model findings at or above
`fix_min_severity` (`medium`) and `fix_min_confidence` (`0.8`) are sent.

Work happens in a scratch copy. Nothing is committed: review with `git diff`, undo with
`git checkout -- .`. `fix` refuses to run on a repo with uncommitted changes (`--allow-dirty`
to override) so that `git diff` shows only its changes; outside git it keeps a backup in
`.helmreview/backup/`. Answers and timeouts are cached in `.helmreview/fix-cache.json`, so a
re-run only asks about files that changed. Use `--no-cache` to ask again.

## Web UI

```
helmreview serve --root ~/git                    # a folder of repos (or one repo)
helmreview serve --root ~/git/infra --root ~/git/platform --open
```

It prints a link with an access token (`http://localhost:8766/?token=…`). Open it once; the token becomes a cookie. From the browser you can:

- pick a repository and run a **scan**, with the same options as the CLI (diff mode, only some charts, model override, skip the model, `helm lint --strict`);
- let it run in the **background**: scans and fixes go into a queue on the server and keep running when you close the page. The **Jobs** button in the top bar shows what is running, what is waiting (with its place in the queue) and what finished, with progress, the log, Cancel/Remove, and download buttons;
- pick the **model** in the top bar: a list of the models your Ollama has (or type one). It applies to every new scan and fix, survives restarts (saved in `~/.cache/helmreview/settings.json`, in the data volume for the container), and a model set in a repo's Options still wins for that repo;
- browse findings by severity, source (HR checks / helm lint / model) or rule, with the suggested fix and the code around each line;
- **Preview fixes** (`fix --dry-run`) and read the fix report and colour diff, then **Apply fixes**. Nothing is ever committed; review with `git diff` as usual.
- **Upload a ZIP** of a chart or chart repository (button or drag and drop). It is unpacked and its scan is queued straight away. **Fix & download** fixes it in the background; when it is done you get a notification with the fixed ZIP. A GitHub "Download ZIP" works as-is.
- **download** the results: every finished job offers its files (fixed ZIP, `fix.patch`, fix report, report as Markdown, JSON or SARIF), and each repo has a **Downloads** tab with the latest ones.

Scans and fixes run as `helmreview scan|fix` subprocesses, so they behave exactly like the CLI and write the same `.helmreview/` reports. Jobs run one at a time, in order, since they share the GPU; up to 20 can wait. The job list lives in the server process, so a restart of `helmreview serve` forgets it (the reports on disk stay).

The UI is a React app (`web/`), built into `src/helmreview/web/static/` and shipped inside the Python package, so a normal install needs no Node and nothing is loaded from a CDN: it works on an air-gapped host.

**Uploads** are unpacked into `~/.cache/helmreview/uploads` (`--upload-dir`), up to 100 MB per ZIP (`--max-upload-mb`), and deleted after 7 days (`--upload-keep-days`, or the Delete button). Each one becomes its own small git repository, so `fix` works on it. Extraction refuses paths that leave the folder, skips symlinks and any `.git`, and stops ZIP bombs (size, file count and compression ratio are checked on the bytes actually written). Uploaded charts are rendered with `helm template` (no cluster access, no plugins), just like a repo under `--root`. `--no-upload` switches uploads off.

**Security.** By default it listens on `127.0.0.1` only and every API call needs the token (`--token` or `$HELMREVIEW_WEB_TOKEN` to fix it, otherwise a random one per start). Only repositories under the `--root` folders are reachable, and file reads cannot leave a repo. The Ollama host, output folder and config file cannot be set from the browser. POSTs need a custom header, so other sites cannot trigger a scan. `--no-auth` is refused unless you bind to localhost. To reach it from another machine, prefer an SSH tunnel (`ssh -L 8766:localhost:8766 gpu-box`). If you do use `--bind 0.0.0.0`, put it behind a TLS reverse proxy, because the token travels in plain HTTP otherwise.

## Docker

Every push to `main` publishes `ghcr.io/jwklijnsma/helmreview` (amd64 + arm64) with helm included. Ollama is not in the image; by default it talks to Ollama on the Docker host.

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

- `--user "$(id -u):$(id -g)"` makes reports and fixes belong to you, not root. The image also runs under arbitrary UIDs (OpenShift).
- On Linux, `--add-host=host.docker.internal:host-gateway` is needed to reach Ollama on the host. Ollama must listen on more than 127.0.0.1 (`OLLAMA_HOST=0.0.0.0` in its service). Or set `-e OLLAMA_HOST=http://gpu-box:11434`.
- Pass settings with `HELMREVIEW_MODEL`, `HELMREVIEW_NUM_CTX` and so on, or mount a config: `-v ~/.helmreview.yml:/home/helmreview/.helmreview.yml:ro`.
- Tags: `latest` and `main` follow the main branch, `0.1.0`/`0.1` come from releases (`make release`), and `sha-abc1234` pins an exact build.

With Ollama on the GPU in the same compose project:

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

Pull the model once with `docker compose exec ollama ollama pull qwen2.5-coder:14b`.

## Run as a service (systemd)

Ready-made units are in [`deploy/systemd/`](deploy/systemd/). They start the container at boot, restart it on failure, log to the journal and keep uploads in a volume.

**Podman (RHEL, Fedora): Quadlet, rootless, as the user who owns the repos**

```bash
openssl rand -hex 24 | tr -d '\n' | podman secret create helmreview-token -   # the access token
mkdir -p ~/.config/containers/systemd
cp deploy/systemd/helmreview.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start helmreview
loginctl enable-linger "$USER"                           # keeps running without a login
systemctl --user enable --now podman-auto-update.timer   # pulls new images daily
```

Show the token again with `podman secret inspect --showsecret --format '{{.SecretData}}' helmreview-token`. Optional settings (model, context size) go in `~/.config/helmreview/helmreview.env`, see `helmreview.env.example`. The repositories come from `~/git`; change the `Volume=%h/git:/repos:z` line for another folder. Reports and fixes stay owned by you (`UserNS=keep-id`). Ollama on the same host is reached as `host.containers.internal`, so it must listen on more than 127.0.0.1 (`systemctl edit ollama` → `Environment=OLLAMA_HOST=0.0.0.0`). For Ollama on another machine, change the unit's `Environment=OLLAMA_HOST=` line.

As a system service run by root instead (in `/etc/containers/systemd/`), use [`helmreview-rootful.container`](deploy/systemd/helmreview-rootful.container); its header has the steps.

**Docker**

```bash
sudo mkdir -p /etc/helmreview
sudo install -m 600 deploy/systemd/helmreview.env.example /etc/helmreview/helmreview.env   # set the token
sudo cp deploy/systemd/helmreview-docker.service /etc/systemd/system/helmreview.service
sudo systemctl edit helmreview     # REPOS=/path/to/repos, RUN_AS=uid:gid of their owner
sudo systemctl enable --now helmreview
```

It pulls the newest image on every restart (`sudo systemctl restart helmreview` updates it).

Both: the UI is on `http://localhost:8766/?token=<your token>` and the logs are in `journalctl [--user] -u helmreview -f`. The server refuses to start while the token is still the example value; without any token it makes up a new random one at every start. Both units drop all capabilities and set no-new-privileges.

## Releasing

Releases run on GitHub. In the repository: **Actions → release → Run workflow**, pick `patch`, `minor` or `major` (or type an exact version), and start it. Or from a terminal with the [GitHub CLI](https://cli.github.com):

```bash
make release                    # 0.1.0 -> 0.1.1, then follows the run
make release BUMP=minor         # 0.1.0 -> 0.2.0   (or BUMP=major)
make release VERSION=1.0.0      # an exact version
make release-dry-run            # rehearse: test and build everything, publish nothing
```

The workflow:
1. checks the new version is higher and not released yet, and runs the tests;
2. bumps `__version__` in `src/helmreview/__init__.py` (the only place the version lives), commits `release: vX.Y.Z` to `main`, and pushes it together with an annotated `vX.Y.Z` tag;
3. builds the wheel and sdist and creates the GitHub release with install lines and notes generated from the merged PRs;
4. builds and smoke-tests the Docker image and publishes `ghcr.io/jwklijnsma/helmreview:X.Y.Z`, `:X.Y` and `:latest`.

A dry run does all of this without the push, the release or the image push, and shows the release notes it would use in the run summary. If you ever push a `vX.Y.Z` tag by hand, it still gets a GitHub release and an image.

## Configure

`helmreview init` writes a sample `.helmreview.yml`. Settings are read in this order, later wins:
built-in defaults, `~/.helmreview.yml`, the repo's `.helmreview.yml`, environment variables, then CLI flags.

| Key | Default | Notes |
|---|---|---|
| `model` | `qwen2.5-coder:14b` | env `HELMREVIEW_MODEL` |
| `ollama_host` | `http://localhost:11434` | env `OLLAMA_HOST`; **ignored in a repo config** |
| `api_key` | – | Bearer token if Ollama sits behind an auth proxy; ignored in a repo config |
| `language` | `en` | `en` or `nl` |
| `num_ctx` | `16384` | context window per request |
| `workers` | `1` | parallel requests; keep it at or below the server's `OLLAMA_NUM_PARALLEL` |
| `timeout` | `600` | max seconds without model output; env `HELMREVIEW_TIMEOUT` |
| `deadline` | `0` | max seconds per request (`0` = 2 × `timeout`) |
| `num_predict` | `4096` | cap on generated tokens; stops a model stuck repeating JSON |
| `keep_alive` | `30m` | keeps the model loaded between requests |
| `per_file` | `true` | one model request per file |
| `values_context_chars` | `6000` | how much of `values.yaml` goes along with a template |
| `fail_on` / `min_severity` | `high` / `info` | `info` `low` `medium` `high` `critical` |
| `min_confidence` | `0.5` | drop model findings it is unsure about |
| `lint_strict` | `false` | `helm lint --strict` (`--strict`) |
| `fix_min_severity` / `fix_min_confidence` | `medium` / `0.8` | which model findings `fix` may touch |
| `fix_batch_size` | `0` | findings per request in `fix`; `0` = all of a file at once (`--batch-size`) |
| `disabled_rules` | `[]` | HR rule IDs or `helm-lint:icon`, `helm-lint:warning`, … |
| `exclude` | sensible list | glob patterns |
| `house_rules` | – | free text the model enforces, e.g. "images only from our registry" |

A repo config cannot change `ollama_host` or `api_key` unless you set
`HELMREVIEW_TRUST_REPO_CONFIG=1`. That way, a repo you cloned cannot send your code to someone
else's server.

## Built-in rules

Run `helmreview rules` for the list.

| Rule | What |
|---|---|
| HR000 | Chart does not render with its default values / YAML error |
| HR001 | Hard-coded secret in `values.yaml` or a Secret template (`{{ }}` placeholders are fine) |
| HR002 / HR004 | Privileged container / runs as root (`runAsUser: 0`) |
| HR003 | Not hardened: `allowPrivilegeEscalation`, `runAsNonRoot`, `readOnlyRootFilesystem`, `drop: [ALL]`, seccomp |
| HR005 / HR009 | No CPU/memory requests or memory limit / no readiness or liveness probe |
| HR006 | Image tag `latest` or none |
| HR007 / HR008 / HR010 | hostNetwork/PID/IPC, hostPath volumes, dangerous capabilities |
| HR011 | `*` in RBAC rules, bindings to cluster-admin |
| HR012 | API versions removed from Kubernetes (`extensions/v1beta1`, `policy/v1beta1`, …) |
| HR013 / HR020 | LoadBalancer/NodePort Services; Ingress or Route without TLS |
| HR014 | Namespace hard-coded in a template instead of `.Release.Namespace` |
| HR015 / HR021 | Private keys committed, credentials in URLs |
| HR016 – HR019 | Chart apiVersion v1, unpinned or plain-HTTP dependencies, no `values.schema.json` |

Findings from rendered objects point at the template they came from (the line is the first
match for the key in that template). Helm test hooks are not held to the hardening, resource
and probe rules. Model findings use the `llm:<rule>` prefix and carry a confidence score.

## Speed

Both `scan` and `fix` send **one request per file** and work through the files in order:
`[12/40] ✓ charts/web/templates/deployment.yaml 41s`. The status of every file (pending, running,
done, cached, failed) is kept in `.helmreview/queue.json`. To watch a long run from another
terminal, use `jq '.counts' .helmreview/queue.json`. Set `per_file: false` to send a whole
chart in one request.

A local model will never match a cloud service, but most slow runs come from a misconfigured
server rather than from the GPU itself. In order of impact:

1. **Run `helmreview doctor`.** It measures prompt and generation speed and estimates how long
   one fix batch and one scan chunk take. It also flags the two big problems:
   - part of the model running on the CPU (often 5–10× slower);
   - the model reloading between requests because another app on the same server uses a
     different model or `num_ctx`.
2. **Tune the server.** Run `sudo scripts/tune-ollama-server.sh --parallel 2` on the Ollama
   host. It turns on flash attention, a q8 KV cache, parallel slots and keep-alive. Then run
   helmreview with `--workers 2`, or set it in `~/.helmreview.yml`.
3. **Re-runs are cheap.** Model answers are cached in `.helmreview/scan-cache.json` and
   `fix-cache.json`, so only changed files go back to the model. Requests that timed out are
   remembered too and are split straight away next time.
4. **Use a smaller model for `fix`.** `helmreview fix --model qwen2.5-coder:7b` is about twice
   as fast. Fixes are small and every edit is verified anyway.

## Tips

- **Model choice.** `qwen2.5-coder:14b` fits a 16 GB card with a 16k context. With 24–32 GB,
  `qwen3-coder:30b` gives noticeably better reviews.
- **Large repos.** Start with `--no-llm` to clear helm lint and HR findings cheaply, then run
  the model.
- **Too many low findings?** `disabled_rules: [HR009, HR019]` or `--min-severity medium`.

## CI

See `examples/gitlab-ci.yml` (Code Quality widget in merge requests). It uses the container
image, which has helm.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use a mock Ollama server (`tests/mock_ollama.py`), so they need no GPU. Tests that render
charts need `helm` on the PATH and are skipped without it.

### Web UI

The React app is in `web/` (Vite, TypeScript). `src/helmreview/web/static/` is its build output and is committed, so after changing `web/src/` run `make web` and commit both. CI rebuilds it and fails when they differ (for Renovate/Dependabot PRs it pushes the rebuilt bundle itself).

```bash
make serve        # API on :8766 (in one terminal)
make web-dev      # http://localhost:5173 with hot reload, /api proxied to :8766
make web-test     # typecheck + vitest
make web          # production build into src/helmreview/web/static/
```
