# This service captures audio PER PARTICIPANT during a Talk recording, then
# transcribes each track on the CPU. It is separate from the Ceffio binary
# because the stack is Python (WebRTC, whisper) while the binary is static.
FROM python:3.12-slim

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
