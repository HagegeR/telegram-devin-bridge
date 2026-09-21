# moonshine-asr sidecar

English-only Moonshine (sherpa-onnx) transcription as a subprocess — zero RAM
when idle; the container starts per request. Outputs plain text without
punctuation; whisper.cpp remains the multilingual option.

Build:

```sh
docker build -t moonshine-asr deploy/moonshine
```

Test:

```sh
ffmpeg -i note.ogg -ar 16000 -ac 1 -f wav - | docker run --rm -i moonshine-asr
```

.env:

```env
TRANSCRIPTION_BACKEND=docker
TRANSCRIPTION_DOCKER_IMAGE=moonshine-asr
```

(or the unrestricted form: `TRANSCRIPTION_BACKEND=command` with
`TRANSCRIPTION_COMMAND=docker run --rm -i --memory 400m moonshine-asr`)

The bridge feeds a 16 kHz mono WAV on stdin and reads the transcript from
stdout; `TRANSCRIPTION_LANGUAGE` is passed to the child's environment.
