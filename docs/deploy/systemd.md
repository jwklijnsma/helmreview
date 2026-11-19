# systemd service

Ready-made units are in
[`deploy/systemd/`](https://github.com/jwklijnsma/helmreview/tree/main/deploy/systemd).
They start the container at boot, restart it on failure, log to the journal and
keep uploads in a volume.

## Podman: one command

For RHEL and Fedora.

```bash
sudo deploy/systemd/install.sh                 # system service, starts at boot, repos in /srv/charts
deploy/systemd/install.sh                      # or rootless, as yourself, repos in ~/git
sudo deploy/systemd/install.sh --repos /srv/git --lan --model qwen3-coder:30b --ollama http://gpu-box:11434
make install-service ARGS="--lan"              # the same from the Makefile
```

Without a checkout:

```bash
curl -fsSL https://raw.githubusercontent.com/jwklijnsma/helmreview/main/deploy/systemd/install.sh | sudo bash -s -- --lan
```

It creates the access token as a Podman secret, writes the settings file
(`/etc/helmreview/helmreview.env`, or `~/.config/helmreview/` rootless) and the
Quadlet unit, runs the container as the owner of the repos folder, starts the
service, waits until it answers and prints the link with the token. Images
update daily through `podman-auto-update.timer`.

| Option | |
|---|---|
| `--repos DIR` | Folder with the repositories (default `/srv/charts` as root, `~/git` otherwise) |
| `--port N` | Port on the host (default 8766) |
| `--lan` | Listen on all interfaces instead of 127.0.0.1 only |
| `--ollama URL` | Ollama server (default: Ollama on this host) |
| `--model NAME` | Default model (can also be picked in the web UI) |
| `--new-token` | Replace the access token |
| `--uninstall` | Stop and remove the service (keeps the token, settings and data) |

Run it again to change the folder, port, model or Ollama URL; the previous unit
is kept as `.bak`. It is safe to repeat.

## By hand

=== "Podman Quadlet (rootless)"

    Runs as the user who owns the repos.

    ```bash
    openssl rand -hex 24 | tr -d '\n' | podman secret create helmreview-token -   # the access token
    mkdir -p ~/.config/containers/systemd ~/.config/helmreview
    cp deploy/systemd/helmreview.container ~/.config/containers/systemd/
    install -m 600 deploy/systemd/helmreview.env.example ~/.config/helmreview/helmreview.env   # must exist
    systemctl --user daemon-reload
    systemctl --user start helmreview
    loginctl enable-linger "$USER"                           # keeps running without a login
    systemctl --user enable --now podman-auto-update.timer   # pulls new images daily
    ```

    - Show the token again with
      `podman secret inspect --showsecret --format '{{.SecretData}}' helmreview-token`.
    - Settings (model, context size) go in `~/.config/helmreview/helmreview.env`.
      The file must exist; an unchanged copy of `helmreview.env.example` is fine.
    - The repositories come from `~/git`. Change the
      `Volume=%h/git:/repos:z` line for another folder.
    - Reports and fixes stay owned by you (`UserNS=keep-id`).
    - Ollama on the same host is reached as `host.containers.internal`, so it
      must listen on more than 127.0.0.1
      (`systemctl edit ollama` → `Environment=OLLAMA_HOST=0.0.0.0`). For Ollama
      on another machine, change the unit's `Environment=OLLAMA_HOST=` line.

=== "Podman Quadlet (root)"

    As a system service run by root, in `/etc/containers/systemd/`, use
    [`helmreview-rootful.container`](https://github.com/jwklijnsma/helmreview/blob/main/deploy/systemd/helmreview-rootful.container).
    Its header has the steps.

=== "Docker"

    ```bash
    sudo mkdir -p /etc/helmreview
    sudo install -m 600 deploy/systemd/helmreview.env.example /etc/helmreview/helmreview.env   # uncomment and set the token
    sudo cp deploy/systemd/helmreview-docker.service /etc/systemd/system/helmreview.service
    sudo systemctl edit helmreview     # REPOS=/path/to/repos, RUN_AS=uid:gid of their owner
    sudo systemctl enable --now helmreview
    ```

    It pulls the newest image on every restart
    (`sudo systemctl restart helmreview` updates it).

## Either way

- The UI is on `http://localhost:8766/?token=<your token>`.
- Logs: `journalctl [--user] -u helmreview -f`.
- The server refuses to start while the token is still the example value;
  without any token it makes up a new random one at every start.
- The units drop all capabilities and set no-new-privileges.

## The settings file

`helmreview.env` has one `KEY=value` per line; comments only on their own line.

```bash
#HELMREVIEW_WEB_TOKEN=              # Docker service only; Podman uses the secret
#HELMREVIEW_MODEL=qwen2.5-coder:14b
#HELMREVIEW_NUM_CTX=16384
#OLLAMA_HOST=http://gpu-box.example:11434
#HELMREVIEW_LANGUAGE=en
#HELMREVIEW_FAIL_ON=high

# Review GitLab merge requests in the background
#HELMREVIEW_WATCH_MRS=1
#HELMREVIEW_GITLAB_URL=https://gitlab.example.nl
#HELMREVIEW_GITLAB_PROJECTS=platform/charts,platform/apps
#GITLAB_TOKEN=
#REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
#GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
```

For Podman, keep the GitLab token out of this file with a secret:

```bash
printf %s 'glpat-...' | podman secret create helmreview-gitlab-token -
# and in helmreview.container:
# Secret=helmreview-gitlab-token,type=env,target=GITLAB_TOKEN
```

See [GitLab merge requests](../usage/merge-requests.md) for the watcher itself.
