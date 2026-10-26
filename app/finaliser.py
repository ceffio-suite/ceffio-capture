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


def speech_chunks(audio, rate=16000):
	"""Speech regions of one track, in samples, with a little padding."""
	from faster_whisper.vad import VadOptions, get_speech_timestamps

	regions = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=400, speech_pad_ms=200))
	return [(int(r["start"]), int(r["end"])) for r in regions]


def transcribe(audio, room_language, model, rate=16000):
	"""Transcribe one track, deciding the language PER SPEECH CHUNK.

	ONE DECISION PER TRACK WAS WRONG, and it was observed: a participant who
	speaks French and ends with one Arabic sentence had the whole track forced
	through the Arabic model, because detection saw 0.66 for Arabic and the
	French majority was lost. A call is not always monolingual — a meeting in
	Algeria certainly is not.

	Each chunk is detected on its own; the room language is the tie-breaker
	when the detection is not confident.
	"""
	lines = []
	for start, end in speech_chunks(audio, rate):
		chunk = audio[start:end]
		if len(chunk) < rate:
			continue
		segments, info = model.transcribe(chunk, language=None, beam_size=5, vad_filter=False)
		language = info.language
		if info.language_probability < 0.6 and room_language:
			language = room_language
			segments, info = model.transcribe(chunk, language=language, beam_size=5, vad_filter=False)
		for s in segments:
			text = s.text.strip()
			if text:
				lines.append({
					"start": start / rate + float(s.start),
					"end": start / rate + float(s.end),
					"text": text,
					"language": language,
				})
	return lines


def envelope(audio, rate=16000, pas=1600):
	"""RMS per 100 ms — the shape of the speech, not its content."""
	import numpy as np

	n = len(audio) // pas * pas
	if n == 0:
		return np.zeros(0, dtype=np.float32)
	return np.sqrt((audio[:n].reshape(-1, pas) ** 2).mean(axis=1))


def measure_offset(mix, track, maximum=30.0, rate=16000):
	"""Find s such that mix[t] looks like track[t + s], in seconds.

	WHY MEASURE INSTEAD OF TRUSTING TIMESTAMPS: the capture joins at the
	"starting" state, up to a dozen seconds before the recorder writes its
	first sample, and that lead differs per participant. On a live recording
	the .srt placed speech at 0 s where the .webm was silent for 12 s — the
	assumption was wrong, the measurement is what the prototype used to get
	6.4 s and 11.2 s.

	Returns (offset, confidence); confidence is the correlation at the best
	shift, and the caller ignores an offset below 0.4.
	"""
	import numpy as np

	em = envelope(mix, rate)
	ep = envelope(track, rate)
	meilleur, meilleur_s = -1.0, 0.0
	pas = int(maximum * 10)
	for d in range(-pas, pas + 1):
		# mix[t] ~ track[t + d/10]
		if d >= 0:
			a, b = em[d:], ep[:len(em) - d]
		else:
			a, b = em[:d], ep[-d:]
		n = min(len(a), len(b))
		if n < 50:
			continue
		a, b = a[:n], b[:n]
		if a.std() == 0 or b.std() == 0:
			continue
		c = float(np.corrcoef(a, b)[0, 1])
		if c > meilleur:
			meilleur, meilleur_s = c, d / 10.0
	return meilleur_s, meilleur


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

	mix = None
	duration = None
	# CEFFIO_MIX is set by the agent, which fetches the recording next to the
	# tracks; the fallback keeps the script usable by hand.
	chemin_mix = os.environ.get("CEFFIO_MIX") or os.path.join(folder, "webm.webm")
	if os.path.exists(chemin_mix):
		mix = pcm(chemin_mix)
		duration = len(mix) / 16000.0

	from faster_whisper import WhisperModel

	model = WhisperModel(MODEL, device="cpu", compute_type="int8",
		cpu_threads=THREADS, download_root=MODELS)

	lines = []
	for track in sorted(glob.glob(os.path.join(folder, "*.opus"))):
		session = os.path.basename(track)[:-len(".opus")]
		who = names.get(session, "participant " + session[:6])
		# Each track opens at its own moment: the WebRTC subscription is not
		# simultaneous, and the gap differs per participant.
		# Measured alignment when the recording is available; the timestamp
		# reference is only a fallback (and it is known to be off by the
		# capture's lead).
		reference = tracks.get(session, webm_start)
		if mix is not None:
			mesure, confiance = measure_offset(mix, pcm(track))
			print("side %s: measured offset %.1fs (confidence %.2f)" % (session[:8], mesure, confiance), flush=True)
			if confiance >= 0.4:
				reference = webm_start + mesure
			else:
				print("  confidence too low, keeping the timestamp reference", flush=True)
		for seg in transcribe(pcm(track), room_language, model):
			start = seg["start"] + reference - webm_start
			end = seg["end"] + reference - webm_start
			if end <= 0:
				continue
			# Tracks keep growing while the call lasts: anything past the
			# recording belongs to another recording (or to nothing).
			if duration is not None and start > duration:
				continue
			lines.append({"start": max(0.0, start), "end": end, "who": who, "text": seg["text"]})

	lines.sort(key=lambda x: x["start"])
	with open(output, "w", encoding="utf-8") as f:
		for i, l in enumerate(lines, 1):
			f.write("%d\n%s --> %s\n%s : %s\n\n" % (i, timestamp(l["start"]), timestamp(l["end"]), l["who"], l["text"]))
	print(json.dumps({"segments": len(lines), "output": output}), flush=True)


if __name__ == "__main__":
	main()
