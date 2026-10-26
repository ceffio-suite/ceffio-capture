"""Finalise a capture: transcription and subtitles, nothing else.

Input : the Opus tracks in /work/pistes (one per participant).
Output: a named .srt, re-aligned on the recording start.

NO INTERMEDIATE FILE IS KEPT: the Opus tracks ARE the capture format, they are
decoded in memory for transcription, and only the subtitles are delivered.

The language is detected PER SPEECH CHUNK: two participants may speak two
different languages in the same call, and one participant may switch mid-sentence.

ALIGNMENT: each track knows when it opened (pistes.json), the recording knows
when it started (its name). The gap between the two is the shift, and it is
within a couple of seconds — the tracks are opened when the call starts.
Measuring the shift by correlating the track against the mix was tried and
REMOVED: on a live recording it answered -16.8 s with a confidence of 0.51, and
the shift pushed one participant's whole transcript before zero, where it was
dropped. A wrong measurement cost the content; the timestamp is wrong by a
second or two, which a subtitle tolerates.
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


def speech_chunks(audio, rate=16000):
	"""Speech regions of one track, in samples, with a little padding."""
	from faster_whisper.vad import VadOptions, get_speech_timestamps

	regions = get_speech_timestamps(audio, VadOptions(
		threshold=0.3, min_silence_duration_ms=400, speech_pad_ms=200))
	return [(int(r["start"]), int(r["end"])) for r in regions]


def transcribe(audio, room_language, model, rate=16000):
	"""Transcribe one track as a WHOLE, in the room language.

	WHY NOT PER SPEECH CHUNK ANY MORE, although it fixed a real case: cutting
	the track into short regions and transcribing each one without context
	made whisper invent text on noise. On a live recording the transcript came
	back as "Merci d'avoir regardé" four times and "Voir une bombe" where the
	same audio, transcribed as a whole, read "C'est un peu bizarre comme truc.
	On va voir si ça marche ou ça marche pas." Short inputs hallucinate.

	The room language is the reference — the moderator declared it, and the
	first version that overrode it per track turned a French speaker who ended
	in Arabic into an all-Arabic transcript. When the room has no language,
	whisper detects it once for the track.
	"""
	segments, info = model.transcribe(audio, language=room_language or None,
		beam_size=5, vad_filter=True)
	language = room_language or info.language
	print("language %s (%.2f)" % (language, info.language_probability), flush=True)
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

	duration = None
	chemin_mix = os.environ.get("CEFFIO_MIX") or os.path.join(folder, "webm.webm")
	if os.path.exists(chemin_mix):
		duration = len(pcm(chemin_mix)) / 16000.0

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
		print("side %s: shift %+.1fs" % (session[:8], reference - webm_start), flush=True)
		for seg in transcribe(pcm(track), room_language, model):
			start = seg["start"] + reference - webm_start
			end = seg["end"] + reference - webm_start
			if end <= 0:
				continue
			# Tracks keep growing while the call lasts: anything clearly past
			# the recording belongs to another recording (or to nothing). A
			# two-second margin, because the shift is an estimate.
			if duration is not None and start > duration + 2:
				continue
			# A slightly early subtitle is better than a missing one: clamping
			# keeps the words, dropping them made an entire participant vanish.
			lines.append({"start": max(0.2, start), "end": end, "who": who, "text": seg["text"]})

	lines.sort(key=lambda x: x["start"])
	with open(output, "w", encoding="utf-8") as f:
		for i, l in enumerate(lines, 1):
			f.write("%d\n%s --> %s\n%s : %s\n\n" % (i, timestamp(l["start"]), timestamp(l["end"]), l["who"], l["text"]))
	print(json.dumps({"segments": len(lines), "output": output}), flush=True)


if __name__ == "__main__":
	main()
