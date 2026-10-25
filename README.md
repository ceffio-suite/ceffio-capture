# ceffio-capture

Captures **one audio track per participant** during a Talk recording, then
transcribes each track on the CPU.

## What it does

When a recording starts in a Talk call, this service joins the call as an
internal client (through the signaling, with the shared secret) and receives
**one audio track per participant** from the SFU — whereas the `.webm` Talk
records is a single mix. When the recording stops:

- one **Opus 32 kbit/s** track per participant (~14 MB/h), next to the `.webm`;
- a **named `.srt`** (who spoke), re-aligned on the recording start;
- the subtitles are **muxed into the `.webm`** (WebVTT track, stream copy).

Transcription uses **faster-whisper in int8, CPU only** (measured ×7–11 real
time on 8 threads), with **per-track language detection**: two participants may
speak two different languages in the same call.

The `small` model is **baked into the image**: a fresh installation transcribes
its first recording without a download.

## Deployment

The image is deployed by the `capture` module of the
[ceffio](https://github.com/ceffio-suite/ceffio) repository:

```sh
ceffio module install capture
```

Expected environment:

| Variable | Role |
|---|---|
| `CEFFIO_HPB_URL` | `wss://spreed.<domain>/spreed` |
| `CEFFIO_NEXTCLOUD_URL` | `https://cloud.<domain>` |
| `CEFFIO_INTERNAL_SECRET` | shared signaling secret |
| `CEFFIO_WORK` | working directory (default `/work`) |
| `CEFFIO_MODELS` | model directory (default `/models`, baked in) |
| `CEFFIO_MODEL` | `small` (default), `medium`, … |
| `CEFFIO_THREADS` | CPU threads for transcription (default 4) |

Kubernetes rights (created by the module): `pods/exec` in the Nextcloud
namespace (deposit files, `occ files:scan`) and in the database namespace
(recording state, room language).

## Build

```sh
docker build --platform linux/amd64 -t ceffio-capture .
```

## Provenance

The signaling client (`app/lib/spreed_client.py` and its modules) comes from
Nextcloud's [live_transcription](https://github.com/nextcloud/live_transcription)
application (AGPL-3.0). It is vendored and adapted: configuration comes from the
environment instead of AppAPI, and the live transcriber is replaced by disk
writes. That path is proven in production — rewriting it would be a risk.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
