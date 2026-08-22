---
name: qwen36-local
description: "Local Qwen3.6-27B MTP on this Mac via llama.cpp — starting llama-server, MTP/sampler flags, wiring agents to the local endpoint, verifying speculative decoding, debugging load/response issues."
---

# Qwen3.6-27B-MTP on this machine

Everything below was **measured on this box on 2026-07-22**, not copied from vendor docs. Where
upstream documentation disagrees with what the hardware actually does, the measurement wins and the
conflict is called out.

## Fixed facts

| Thing | Value |
|---|---|
| Model | `~/models/gguf/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q6_K_XL.gguf` (24 GiB, Q6_K) |
| Vision projector | `~/models/gguf/Qwen3.6-27B-MTP-GGUF/mmproj-F16.gguf` (885 MB) |
| Binaries | `~/llama.cpp/build/bin/` (also copied to `~/llama.cpp/`) |
| Build | `b1-0278d83`, AppleClang, Metal + BLAS(Accelerate), `-DGGML_CUDA=OFF` |
| Hardware | M4 Max, 16-core CPU / 40-core GPU, 128 GB unified |
| Model footprint | ~25.5 GB RSS at 8K ctx |
| Warm load time | ~5 s (page cache warm); first-ever load is minutes |
| Reference doc | `~/models/gguf/Qwen3.6-27B-MTP-GGUF/RUNBOOK.md` |

Rebuild after `git pull` with:
```bash
cmake -B build -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=OFF -DCMAKE_BUILD_TYPE=Release   # in ~/llama.cpp
cmake --build build --config Release -j 16 --target llama-cli llama-mtmd-cli llama-server llama-gguf-split llama-bench
```

## Default: start the server

This is the recommended config — MTP on at `n-max 1`, vision loaded, thinking off (agent-friendly).

```bash
~/llama.cpp/build/bin/llama-server \
  --model ~/models/gguf/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q6_K_XL.gguf \
  --mmproj ~/models/gguf/Qwen3.6-27B-MTP-GGUF/mmproj-F16.gguf \
  -ngl 99 --alias qwen36 --ctx-size 16384 --port 8001 --host 127.0.0.1 -np 1 \
  --spec-type draft-mtp --spec-draft-n-max 1
```

Drop `--mmproj` if you don't need images (saves ~1 GB and shortens load).

### Readiness — do NOT poll `/health` alone

`/health` returns `{"status":"ok"}` **while the model is still loading**; requests sent then fail with
`HTTP 503`. This cost a benchmark run. Correct gate:

```bash
until grep -q "model loaded" /tmp/srv.log; do sleep 3; done
until curl -s -m 120 -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' | grep -q 200; do sleep 3; done
```

## Verify MTP is actually running

Never assume the flags took. Two independent proofs:

1. **Per-response telemetry** — the `timings` block gains `draft_n` / `draft_n_accepted`. Absent or
   zero ⇒ MTP is off.
   ```bash
   curl -s http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"m","messages":[{"role":"user","content":"Count 1 to 20."}],"max_tokens":100}' \
     | python3 -c 'import json,sys; t=json.load(sys.stdin)["timings"]; print("draft_n",t.get("draft_n"),"accepted",t.get("draft_n_accepted"))'
   ```
2. **Server log** — `draft acceptance = 0.87013 ( 67 accepted / 77 generated), mean len = 1.87`.

## `--spec-draft-n-max`: use 1 (or 2). Never ≥3.

Measured with interleaved A/B (both arms in every round, greedy decode, 4 reps × 2 rounds):

| config | round 1 | round 2 | draft acceptance | vs baseline |
|---|---|---|---|---|
| no MTP | 9.19 tok/s | 8.96 tok/s | — | 1.00× |
| **MTP n=1** | **11.75** | **10.81** | **87.0%** | **~1.24×** |
| MTP n=2 | 11.08 | 10.81 | 75.4% | ~1.20× |
| MTP n=3 | — | — | 66.7% | slower than baseline |
| MTP n=4 | — | — | 61.3% | slower than baseline |
| MTP n=6 | — | — | 47.8% | much slower (~0.7×) |

- **Upstream says "2 works best in most setups." On this Mac, 1 is best.** Their own caveat — "do not
  assume 2 is optimal, performance is hardware-dependent" — is the correct guidance.
- Acceptance rate is **deterministic under greedy decode**: 87.0% / 75.4% reproduced exactly across
  independent runs. If you re-measure and get different acceptance for the same n, something else
  changed (model, prompt, or sampler).
- Peak observed throughput on an idle machine: **~22 tok/s**.

### Benchmarking rule

Absolute tok/s on this box swings **2-3×** with background load — `ollama` holding the GPU (the
`scout_daemon.py` research loop wakes it) and the Docker VM (25 GB RSS, bursts to 300% CPU) both
matter. A single before/after pair is worthless. Always **interleave arms within rounds** and report
medians. Check contention first with `ollama ps` and `ps -eo pid,%cpu,rss,comm | sort -k3 -rn | head`.

To prove a change didn't alter output, use `temperature: 0.0, top_k: 1` and hash the completion — MTP
on/off/any-n produced a byte-identical sha here, which is the empirical confirmation of the
"speculative decoding does not change output" claim.

## Why throughput is what it is — read before "optimizing"

**Decode is memory-bandwidth-bound, and this machine is already running at ~72% of its hardware
ceiling.** There is no configuration cap to remove.

Each generated token streams the entire weight set through memory once, so
`tok/s ≈ effective_bandwidth ÷ model_bytes`. Measured with `llama-bench` on an idle GPU:

| model | size | tg128 | effective bandwidth |
|---|---|---|---|
| Qwen3.6-27B Q6_K | 26.02 GB | **14.98 tok/s** | 389.7 GB/s (71.4% of 546) |
| Qwen3.5-9B Q6_K | 7.36 GB | **53.94 tok/s** | 396.9 GB/s (72.7% of 546) |

Two models 3.5× apart in size converge on the *same* effective bandwidth — that is the definitive
memory-bandwidth-bound signature. M4 Max peak is 546 GB/s, so the absolute ceiling for this 26 GB
file is **20.99 tok/s** and ~15 is the realistic number. Prompt processing is a different regime
(compute-bound): pp512 = 228 tok/s for the 27B, 791 tok/s for the 9B.

**This runs on the Mac's GPU, natively.** `llama-server` is a Mach-O arm64 binary linked against
`Metal.framework`; `-ngl 99` puts every layer on the 40-core GPU. Docker is *not* in the inference
path — it is only a noisy neighbour (58 containers, 26.4 GB VM). Measured proof, same model and
machine, Q4_K_XL:

| | pp128 | tg64 | effective BW |
|---|---|---|---|
| `-ngl 99` (Metal GPU) | 224.5 tok/s | **22.16 tok/s** | ~397 GB/s |
| `-ngl 0` (CPU only) | 38.4 tok/s | 10.04 tok/s | ~180 GB/s |

So the GPU is 2.2× the CPU for decode and 5.8× for prefill — and the GPU's ~397 GB/s effective
already **exceeds an RTX 4000 Ada's 360 GB/s theoretical peak**. Never assume a discrete GPU beats
this machine; compare bandwidth numbers, not the word "GPU".

**Correction — size alone over-predicts.** Pure `bandwidth ÷ size` said `UD-Q4_K_XL` would be 1.45×
the 6-bit. **Measured: ~1.25×.** Q4_K costs more dequantization compute per byte, so it achieves only
~86% of the effective bandwidth Q6_K does (Q6 355 GB/s vs Q4 307 GB/s under identical conditions).
Corrected model: `tok/s ≈ 390 GB/s × quant_efficiency ÷ size_GB`, with Q6_K ≈ 1.00 and Q4_K ≈ 0.86.
Treat sub-4-bit and IQ-type projections as unverified — they are more dequant-heavy still.

**Consequences:**
- Threads, `-ngl`, batch size and flash-attention tuning will not move decode speed. Don't bother.
- **Quant size is still the throughput dial**, just with a smaller slope than naive division implies.
  Measured head-to-head with MTP on, same prompt, same conditions: `UD-Q6_K_XL` 13.65 tok/s vs
  **`UD-Q4_K_XL` 17.15 tok/s (~1.25×)**, Q4 peak 24.14.
- **Smaller quants are also more stable.** Under memory pressure Q6 swung 5.87–9.09 tok/s between
  runs while Q4 held 11.21–11.55. At 26 GB the 6-bit competes with the ~26 GB Docker VM for RAM;
  the 18 GB file has slack. On this box that resilience is worth as much as the raw speed.
- **MTP is the only way to beat the roofline**, because verified draft tokens ride along on a single
  weight stream. That is why ~22 tok/s was observed with MTP against a 20.99 single-token ceiling.
- Vendor numbers (160 tok/s on an RTX 6000) are a *bandwidth* story, not a software one — that card
  has roughly 3× the memory bandwidth and the quote is at a smaller quant.
- Any reading well below ~15 tok/s means contention, not misconfiguration. Check `ollama ps` and the
  Docker VM before touching flags.

## Sampler settings — three distinct presets

| | Thinking, general | Thinking, precise coding | Instruct (non-thinking) |
|---|---|---|---|
| temperature | 1.0 | 0.6 | 0.7 |
| top_p | 0.95 | 0.95 | 0.8 |
| top_k | 20 | 20 | 20 |
| min_p | 0.0 | 0.0 | 0.0 |
| presence_penalty | 0.0 | 0.0 | 1.5 |
| repeat_penalty | off / 1.0 | off / 1.0 | off / 1.0 |

## Thinking control — default is ON

Verified by rendering prompts through `POST /apply-template`:

| kwargs | rendered assistant header | meaning |
|---|---|---|
| *(none)* | `<|im_start|>assistant\n<think>\n` | **thinking ON — this is the default** |
| `{"enable_thinking":true}` | identical to default | confirms default |
| `{"enable_thinking":false}` | `<think>\n\n</think>\n\n` | pre-closed empty block ⇒ thinking suppressed |

**For any agent/tool-calling workload, pass `enable_thinking:false` explicitly.** Otherwise every
call burns reasoning tokens.

```json
"chat_template_kwargs": {"enable_thinking": false}
```

Thinking traces come back in `message.reasoning_content`, not `message.content`.

### Trap: empty `content` in thinking mode

A trivial arithmetic question with `max_tokens: 600` returned **`content: ""` and 1398 chars of
`reasoning_content`** — the budget was consumed mid-reasoning, `finish_reason: length`. At 2048 it
answered correctly. **In thinking mode budget ≥2048 output tokens**, and if `content` is empty check
`finish_reason` before assuming the model failed.

### Preserve-thinking (keep traces across turns)

Both of these work and render **byte-identically**:
- `--reasoning-preserve` (llama.cpp flag) — **prefer this**
- `"chat_template_kwargs": {"preserve_thinking": true}` (Unsloth's documented form)

Reason the vendor-neutral flag works on a template that never mentions `preserve_reasoning`:
`caps_apply_preserve_reasoning()` in `common/jinja/caps.cpp` sets four template variables at once —
`preserve_thinking`, `clear_thinking`, `truncate_history_thinking`, `drop_thinking`. Passing the raw
`preserve_thinking` kwarg only sets one of them, so the flag is the more robust choice.

## Two model-card warnings that are FALSE on this build

The HF model card says: *"`-np > 1` and `--mmproj` are not yet supported with MTP."* Both halves were
tested and both are stale — llama.cpp `b1-0278d83` handles them fine.

- **Vision + MTP:** server loads both (`capabilities: ['completion','multimodal']`), image description
  was fully correct (OCR'd rendered text, named the true colour rather than the colour implied by the
  word, located both shapes), and **MTP stayed active during multimodal inference** (`draft_n=127,
  accepted=111`, 17.7 tok/s).
- **`-np 4` + MTP:** 4 slots initialised, 4 concurrent requests served in 5.1 s wall, every slot
  drafting. Note `--ctx-size` is **divided across slots** — `--ctx-size 16384 -np 4` ⇒ 4096 per slot.

Use `-np 1` for single-user interactive work anyway (full context to one slot); raise it only for
genuine concurrency.

## llama-cli: use `-st`, never `-no-cnv`

```bash
~/llama.cpp/build/bin/llama-cli \
  --model ~/models/gguf/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q6_K_XL.gguf \
  -ngl 99 -st -p "your prompt" -n 256 --temp 0.7 --top-p 0.8 --top-k 20 \
  --ctx-size 8192 --spec-type draft-mtp --spec-draft-n-max 1 \
  --chat-template-kwargs '{"enable_thinking":false}' < /dev/null
```

⚠️ **`-no-cnv` disables conversation mode but does NOT disable interactive mode.** Run it with stdin
at `/dev/null` and llama-cli spins on EOF emitting `> ` forever — this wrote **402 MB** of prompt
characters in ~4 minutes before being killed. `-st` (`--single-turn`) is the correct
"one prompt, print, exit" flag; it exits 0.

## Wiring a coding agent

`--alias` sets the id served at `GET /v1/models`; the client's model name must match it exactly.

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8001/v1", api_key="sk-no-key-required")
r = c.chat.completions.create(
    model="qwen36",                                   # == --alias
    messages=[{"role": "user", "content": "..."}],
    temperature=0.7, top_p=0.8, presence_penalty=1.5,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
```

**Tool calling is verified working**: a single-tool request returned `finish_reason: "tool_calls"`
with correctly typed nested arguments (`{"city":"Tokyo","unit":"c"}`), enum honoured, MTP active
throughout. Qwen3.6 uploads also add `developer` role support for Codex/OpenCode.

## Failure playbook

| Symptom | Cause | Fix |
|---|---|---|
| `HTTP 503` right after start | polled `/health`, model still loading | wait for `model loaded` in log + a real 200 |
| CLI never exits, huge output file | `-no-cnv` leaves interactive on | use `-st`, redirect stdin from `/dev/null` |
| `content` empty, only reasoning | thinking ate the token budget | `max_tokens ≥ 2048` or `enable_thinking:false` |
| `draft_n` absent from timings | MTP flags not applied / stale binary | check both flags; rebuild if `--spec-type` errors |
| Throughput halved run-to-run | ollama/Docker contention | `ollama ps`; interleave benchmark arms |
| Gibberish output | context set too low | raise `--ctx-size`; or `--cache-type-k bf16 --cache-type-v bf16` |
| Model re-downloads on start | used `-hf repo:quant` instead of `--model` | always pass the local path |

## Things NOT to do

- Don't copy upstream commands verbatim — they use `UD-Q4_K_XL` and `-hf` auto-download, which
  fetches a **second** copy (~18 GB) into `LLAMA_CACHE` and ignores the local 6-bit file.
- Don't pass `-DGGML_CUDA=ON` here; Metal is independent and on by default.
- Don't set `--spec-draft-n-max` above 2 on this hardware.
- Don't kill `scout_daemon.py` or the ollama server to get a clean benchmark — that's Chad's running
  research loop. Measure around it with interleaving.
