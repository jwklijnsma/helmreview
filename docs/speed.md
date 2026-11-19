# Speed and tuning

Both `scan` and `fix` send **one request per file** and work through the files
in order: `[12/40] ✓ charts/web/templates/deployment.yaml 41s`. The status of every file
(pending, running, done, cached, failed) is kept in `.helmreview/queue.json`. To
watch a long run from another terminal:

```bash
jq '.counts' .helmreview/queue.json
```

Set `per_file: false` to send a whole chart in one request.

A local model will never match a cloud service, but most slow runs come from a
misconfigured server rather than from the GPU itself. In order of impact:

## 1. Run helmreview doctor

```bash
helmreview doctor --host http://gpu-box:11434
```

It measures prompt and generation speed and estimates how long one fix batch and
one scan chunk take. It also flags the two big problems:

- **part of the model running on the CPU**, often 5–10× slower. Use a smaller
  model or a smaller `num_ctx`, or free VRAM;
- **the model reloading between requests**, because another app on the same
  server uses a different model or `num_ctx`.

`--no-speed` skips the speed test.

## 2. Tune the Ollama server

On the Ollama host:

```bash
sudo scripts/tune-ollama-server.sh --parallel 2
```

It turns on flash attention, a q8 KV cache, parallel slots and keep-alive. Then
run helmreview with `--workers 2`, or set `workers: 2` in `~/.helmreview.yml`.
Keep `workers` at or below the server's `OLLAMA_NUM_PARALLEL`: queued requests
count against `timeout`.

## 3. Re-runs are cheap

Model answers are cached in `.helmreview/scan-cache.json` and `fix-cache.json`,
so only changed files go back to the model. Requests that timed out are
remembered too and are split straight away next time.

## 4. Use a smaller model for fix

```bash
helmreview fix --model qwen2.5-coder:7b
```

About twice as fast. Fixes are small and every edit is verified anyway.

## Model choice

| VRAM | Model |
|---|---|
| 16 GB | `qwen2.5-coder:14b` with a 16k context (the default) |
| 24–32 GB | `qwen3-coder:30b`, for noticeably better reviews |

## Timeouts

| Setting | Default | Meaning |
|---|---|---|
| `timeout` | `600` | Longest the model may stay silent. Responses are streamed, so this is not the total time |
| `deadline` | `0` | Longest one request may take in total (`0` = 2 × `timeout`) |
| `num_predict` | `4096` | Cap on generated tokens; stops a model stuck repeating JSON |
| `keep_alive` | `30m` | Keeps the model loaded between requests |
