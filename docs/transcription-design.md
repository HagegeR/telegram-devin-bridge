# Transcription design: why these backends

Voice notes should become text before they reach Devin, on a host that may be
a small VM (< 4 cores, < 2 GB RAM, no AVX) running Alpine/musl. This note
records the measurements behind the current backend set so the choice is not
re-litigated from scratch. Configuration lives in
[configuration.md](configuration.md#transcription).

## Constraints

- **musl**: faster-whisper / CTranslate2 have no Alpine wheels, so the `local`
  backend is unavailable there; whisper.cpp builds from source.
- **No AVX**: whisper's encoder is compute-bound and several times slower than
  real time on such CPUs.
- **RAM**: nothing may stay resident between voice notes; a persistent ASR
  sidecar would pin ~250 MB on a machine with a few hundred MB of headroom.
- **Safety**: an admin can change settings through `/admin`, so any backend
  that executes code must not let a settings change pick arbitrary code.

## Measurements

Same 11 s English clip, 3 threads, Celeron-class CPU (SSE4.2 only):

| Engine | Time | Output |
| --- | --- | --- |
| whisper.cpp `base.en` | 76 s | reference |
| whisper.cpp `base.en` + flash-attn | 76 s | same (no gain without AVX) |
| whisper.cpp `base.en-q5_1` (quantized) | 33 s | identical text |
| whisper.cpp `tiny.en-q5_1` | 14 s | worse punctuation/names |
| whisper.cpp `base.en-q5_1` + `WHISPER_CPP_FAST` | ~13 s | one word off |
| Moonshine base int8 (sherpa-onnx), warm | 2 s | correct, no punctuation |
| Moonshine, one-shot container end-to-end | 7–8 s | ~5 s container start + model load |
| Telegram Opus → ffmpeg → Moonshine container | ~10 s | correct |

## Decisions

1. **`whispercpp` stays the multilingual fallback.** Quantized `*-q5_1`
   models cost nothing in accuracy here and halve the time; download them
   instead of the fp16 files.
2. **`WHISPER_CPP_FAST` defaults on**: greedy decoding (`-bs 1 -bo 1`) plus an
   audio context sized to the clip (`-ac ≈ 1500 × duration / 30 s`, capped at
   1500) is where the remaining ~2.5× comes from; it scales better the shorter
   the note. `WHISPER_CPP_EXTRA_ARGS` is validated so it cannot redirect
   input, model or output files.
3. **Moonshine runs as a one-shot container, not a server.** sherpa-onnx is
   glibc-only, so it cannot live in the Alpine venv; a container is required
   anyway, and starting it per request (`docker run --rm -i`, WAV on stdin,
   text on stdout) keeps idle RAM at zero. The ~5 s startup is still 4× faster
   than the best whisper.cpp setting. Trade-offs: English-only, no
   punctuation.
4. **`docker` is the bounded form of `command`.** The argv is fixed in code
   (`--pull never --network none --cap-drop ALL --security-opt
   no-new-privileges --pids-limit 64 --memory <MEM>`), the image reference is
   validated and root-only, only the memory cap is admin-settable, and
   `docker rm -f <NAME>` runs on timeout, failure or cancellation. Decoded
   audio is capped in duration, child stdout/stderr are capped, subprocesses
   run in their own process group, and concurrent transcriptions are limited
   by a semaphore. `TRANSCRIPTION_COMMAND` remains the unrestricted,
   `.env`-only escape hatch.
5. **`api` remains the fast path when sending audio off-box is acceptable.**
