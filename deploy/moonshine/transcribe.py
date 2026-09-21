"""Moonshine (sherpa-onnx) transcription sidecar: WAV on stdin -> text on stdout."""

from __future__ import annotations

import io
import os
import sys
import wave


def main() -> int:
    try:
        import numpy as np
        import sherpa_onnx

        with wave.open(io.BytesIO(sys.stdin.buffer.read()), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                print("expected 16-bit PCM mono WAV", file=sys.stderr)
                return 2
            rate = wav.getframerate()
            if rate != 16000:
                print(f"expected 16000 Hz, got {rate}", file=sys.stderr)
                return 2
            frames = wav.readframes(wav.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        model_dir = os.environ.get("MODEL_DIR", "/model")
        rec = sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=f"{model_dir}/preprocess.onnx",
            encoder=f"{model_dir}/encode.int8.onnx",
            uncached_decoder=f"{model_dir}/uncached_decode.int8.onnx",
            cached_decoder=f"{model_dir}/cached_decode.int8.onnx",
            tokens=f"{model_dir}/tokens.txt",
            num_threads=int(os.environ.get("ASR_THREADS", "3")),
        )
        stream = rec.create_stream()
        stream.accept_waveform(16000, samples)
        rec.decode_stream(stream)
        print(stream.result.text.strip())
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"transcription failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
