"""Finalise a capture: transcription and subtitles, nothing else.

Input : the Opus tracks in /work/pistes (one per participant).
Output: a named .srt, re-aligned on the recording start.

NO INTERMEDIATE FILE IS KEPT: the Opus tracks ARE the capture format, they are
decoded in memory for transcription, and only the subtitles are delivered. A
WAV would only multiply disk usage by ten for a format nobody ships.

The language is detected PER TRACK: two participants may speak two different
languages in the same call.
"""
import glob
import json
import os
import sys

MODELS = os.environ.get("CEFFIO_MODELS", "/models")
MODEL = os.environ.get("CEFFIO_MODEL", "small")
THREADS = int(os.environ.get("CEFFIO_THREADS", "4"))


def pcm(path):
	"""Opus 48 kHz -> mono 16 kHz float32 PCM, in memory."""
	import av
	import numpy as np

	with av.open(path) as container:
		track = next(p for p in container.streams if p.type == "audio")
		resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
		blocks = []
		for frame in container.decode(track):
			for out in resampler.resample(frame):
				blocks.append(out.to_ndarray().reshape(-1))
		for out in resampler.resample(None):
			blocks.append(out.to_ndarray().reshape(-1))
	if not blocks:
		return np.zeros(0, dtype=np.float32)
	return np.concatenate(blocks).astype(np.float32) / 32768.0


def transcribe(audio, room_language, model):
	segments, info = model.transcribe(audio, language=None, beam_size=5, vad_filter=True)
	print("detected language: %s (%.2f)" % (info.language, info.language_probability), flush=True)
	if info.language_probability < 0.5 and room_language:
		print("  low confidence -> room language: %s" % room_language, flush=True)
		segments, info = model.transcribe(audio, language=room_language, beam_size=5, vad_filter=True)
	return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
		for s in segments if s.text.strip()]


def timestamp(t):
	h, rest = divmod(max(0.0, t), 3600)
	m, s = divmod(rest, 60)
	return "%02d:%02d:%02d,%03d" % (int(h), int(m), int(s), int((t - int(t)) * 1000))


def main():
	folder, output = sys.argv[1], sys.argv[2]
	webm_start = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
	room_language = os.environ.get("CEFFIO_LANGUE", "")

	tracks = {}
	path = os.path.join(folder, "pistes.json")
	if os.path.exists(path):
		tracks = json.load(open(path))

	names = {}
	path = os.path.join(folder, "noms.json")
	if os.path.exists(path):
		names = json.load(open(path))

	from faster_whisper import WhisperModel

	model = WhisperModel(MODEL, device="cpu", compute_type="int8",
		cpu_threads=THREADS, download_root=MODELS)

	lines = []
	for track in sorted(glob.glob(os.path.join(folder, "*.opus"))):
		session = os.path.basename(track)[:-len(".opus")]
		who = names.get(session, "participant " + session[:6])
		# Each track opens at its own moment: the WebRTC subscription is not
		# simultaneous, and the gap differs per participant.
		reference = tracks.get(session, webm_start)
		for seg in transcribe(pcm(track), room_language, model):
			start = seg["start"] + reference - webm_start
			end = seg["end"] + reference - webm_start
			if end <= 0:
				continue
			lines.append({"start": max(0.0, start), "end": end, "who": who, "text": seg["text"]})

	lines.sort(key=lambda x: x["start"])
	with open(output, "w", encoding="utf-8") as f:
		for i, l in enumerate(lines, 1):
			f.write("%d\n%s --> %s\n%s : %s\n\n" % (i, timestamp(l["start"]), timestamp(l["end"]), l["who"], l["text"]))
	print(json.dumps({"segments": len(lines), "output": output}), flush=True)


if __name__ == "__main__":
	main()
