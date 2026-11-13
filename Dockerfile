# This service captures audio PER PARTICIPANT during a Talk recording, then
# transcribes each track on the CPU. It is separate from the Ceffio binary
# because the stack is Python (WebRTC, whisper) while the binary is static.
#
# THE BASE IMAGE IS PINNED BY DIGEST, like every image of the suite (see
# internal/version in the Ceffio repository). "python:3.12-slim" moves: a
# rebuild months apart would ship a different interpreter and a different
# Debian snapshot, with no commit saying so — which is exactly what a
# reproducible appliance cannot afford. The digest below is the
# MULTI-ARCHITECTURE INDEX, so the build keeps resolving linux/amd64 by
# platform instead of being frozen to one architecture.
#
# 3.12.16 (2026-11-05): this interpreter carries the fix of CVE-2026-115557,
# which the image scan reported against 3.12.15 and which blocked the v0.8.4
# image.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# ffmpeg: muxing subtitles into the .webm (stream copy, no re-encode).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

# THE MODEL IS BAKED IN, ON PURPOSE. A self-hosted appliance must transcribe
# its first recording without reaching the internet, and a first call that
# waits for a download is a call that fails. A larger model (medium) can still
# be fetched later into the /models-extra volume for better dialect coverage.
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8', download_root='/models')"

ENV CEFFIO_WORK=/work \
    CEFFIO_MODELS=/models \
    CEFFIO_MODEL=small

EXPOSE 8080

# The signaling client comes from the transcription app (AGPL, vendored in
# app/lib with its provenance): that path is proven, and rewriting it would be
# a risk, not a simplification.
CMD ["python3", "/app/agent.py"]
