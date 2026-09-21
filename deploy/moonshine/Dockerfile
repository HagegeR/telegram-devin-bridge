FROM python:3.12-slim

RUN pip install --no-cache-dir sherpa-onnx numpy

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl bzip2 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/model.tar.bz2 \
       https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2 \
    && mkdir -p /model \
    && tar -xjf /tmp/model.tar.bz2 -C /model --strip-components=1 \
    && rm /tmp/model.tar.bz2 \
    && find /model -mindepth 1 -maxdepth 1 \
       ! -name preprocess.onnx \
       ! -name encode.int8.onnx \
       ! -name uncached_decode.int8.onnx \
       ! -name cached_decode.int8.onnx \
       ! -name tokens.txt \
       -exec rm -rf {} +

WORKDIR /app
COPY transcribe.py /app/transcribe.py

ENV OMP_NUM_THREADS=3
RUN useradd -r -u 10001 appuser
USER appuser

ENTRYPOINT ["python", "/app/transcribe.py"]
