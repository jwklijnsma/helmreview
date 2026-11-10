"""`helmreview doctor` speed check: measure the server and point at the usual bottlenecks."""

from __future__ import annotations

from .config import Config
from .ollama import OllamaClient

# ~2.5k tokens of a realistic chart template, so prompt speed is measured on a real-sized prompt
_TASK = """---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{{{ include "app.fullname" . }}}}-config-{i}
  labels:
    {{{{- include "app.labels" . | nindent 4 }}}}
data:
  key{i}: {{{{ .Values.config.key{i} | quote }}}}
"""
_PROMPT = ("Count the Kubernetes objects in this template and list ten short words about Helm.\n\n"
           + "".join(_TASK.format(i=i) for i in range(45)))
_SCHEMA = {"type": "object",
           "properties": {"objects": {"type": "integer"},
                          "words": {"type": "array", "items": {"type": "string"}}},
           "required": ["objects", "words"]}

FIX_REQ = (3000, 500)     # typical fix batch: prompt tokens, output tokens
SCAN_REQ = (7000, 1200)   # typical scan chunk


def _rate(count, ns) -> float:
    return (count or 0) / (ns / 1e9) if ns else 0.0


def speed_check(cfg: Config) -> tuple[list[str], bool]:
    """Returns (lines to print, ok)."""
    c = OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key, timeout=int(cfg.timeout),
                     retries=0, temperature=0.0, num_ctx=int(cfg.num_ctx), num_predict=256,
                     keep_alive=str(cfg.keep_alive))
    out, ok = [], True
    c.chat_json("Return only JSON.", _PROMPT, _SCHEMA)            # warm-up / load
    cold_load = (c.last_stats.get("load_duration") or 0) / 1e9
    # change the prompt so nothing is served from the server's prompt cache
    c.chat_json("Return only JSON.", _PROMPT.replace("Count", "Please count"), _SCHEMA)
    st = c.last_stats
    pp = _rate(st.get("prompt_eval_count"), st.get("prompt_eval_duration"))
    tg = _rate(st.get("eval_count"), st.get("eval_duration"))
    reload = (st.get("load_duration") or 0) / 1e9
    out.append(f"✓ speed: prompt {pp:,.0f} tok/s · generation {tg:,.1f} tok/s "
               f"(model load {cold_load:.1f}s)")
    if pp and tg:
        fix_s = FIX_REQ[0] / pp + FIX_REQ[1] / tg
        scan_s = SCAN_REQ[0] / pp + SCAN_REQ[1] / tg
        out.append(f"  ≈ {fix_s:.0f}s per fix batch, {scan_s:.0f}s per scan chunk "
                   f"(× requests ÷ workers={cfg.workers})")
    if reload > 1.0:
        ok = False
        out.append(f"✗ the model was reloaded between two requests ({reload:.1f}s). Something "
                   "else on the server uses another model or num_ctx (each switch reloads). "
                   "Give helmreview its own server, match num_ctx, or set OLLAMA_MAX_LOADED_MODELS=2.")

    try:
        loaded = [m for m in c.running() if m.get("name") in (cfg.model, cfg.model + ":latest")
                  or m.get("model") in (cfg.model, cfg.model + ":latest")]
    except Exception:
        loaded = []
    if loaded:
        m = loaded[0]
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        if size and vram < size * 0.98:
            ok = False
            cpu = 100 * (size - vram) / size
            out.append(f"✗ {cpu:.0f}% of the model runs on the CPU ({vram / 2**30:.1f} of "
                       f"{size / 2**30:.1f} GiB on the GPU). This is usually THE slowdown. "
                       "Lower num_ctx or OLLAMA_NUM_PARALLEL, enable OLLAMA_FLASH_ATTENTION=1 "
                       "and OLLAMA_KV_CACHE_TYPE=q8_0, or use a smaller quant/model.")
        else:
            out.append(f"✓ model fully on the GPU ({vram / 2**30:.1f} GiB)")
    if tg and tg < 15:
        out.append("  tip: generation under 15 tok/s on a GPU usually means CPU offload, "
                   "a model too big for the card, or another job on the GPU (nvidia-smi).")
    out.append("  tip: run scripts/tune-ollama-server.sh on the Ollama host "
               "(flash attention, q8 KV cache, parallel slots).")
    return out, ok
