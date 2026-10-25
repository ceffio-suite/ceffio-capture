"""Finalise une capture : pistes Opus, transcription, sous-titres.

faster-whisper en int8 (CPU uniquement). La langue est détectée PAR PISTE :
deux participants peuvent parler deux langues différentes dans le même appel.
La langue de la salle ne sert que de filet quand la confiance est basse.

Chaque sous-titre est recalé sur l'instant de départ du .webm, en tenant
compte de la date d'ouverture propre à chaque piste (l'abonnement WebRTC n'est
pas simultané).

Usage : finaliser.py <dossier_pistes> <srt_de_sortie> <epoch_debut_webm>
"""
import glob
import json
import os
import sys

MODELES = os.environ.get("CEFFIO_MODELS", "/models")
MODELE = os.environ.get("CEFFIO_MODEL", "small")
THREADS = int(os.environ.get("CEFFIO_THREADS", "4"))


def pcm(chemin):
	import wave

	import numpy as np

	with wave.open(chemin, "rb") as w:
		n = w.getnframes()
		return np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0


def transcrire(chemin, langue, modele):
	audio = pcm(chemin)
	segments, info = modele.transcribe(audio, language=None, beam_size=5, vad_filter=True)
	print("langue %s (%s) confiance %.2f" % (os.path.basename(chemin)[:10], info.language, info.language_probability), flush=True)
	if info.language_probability < 0.5 and langue:
		print("  confiance faible -> langue de la salle %s" % langue, flush=True)
		segments, info = modele.transcribe(audio, language=langue, beam_size=5, vad_filter=True)
	return [{"debut": float(s.start), "fin": float(s.end), "texte": s.text.strip()} for s in segments if s.text.strip()]


def encoder_opus(src, dst):
	import av

	with av.open(dst, "w", format="ogg") as sortie:
		piste = sortie.add_stream("libopus", rate=48000)
		piste.layout = "mono"
		piste.bit_rate = 32000
		with av.open(src) as entree:
			s = next(x for x in entree.streams if x.type == "audio")
			res = av.AudioResampler(format="s16", layout="mono", rate=48000)
			for frame in entree.decode(s):
				for o in res.resample(frame):
					o.pts = None
					for paquet in piste.encode(o):
						sortie.mux(paquet)
			for o in res.resample(None):
				o.pts = None
				for paquet in piste.encode(o):
					sortie.mux(paquet)
		for paquet in piste.encode(None):
			sortie.mux(paquet)


def horodate(t):
	h, reste = divmod(max(0.0, t), 3600)
	m, s = divmod(reste, 60)
	return "%02d:%02d:%02d,%03d" % (int(h), int(m), int(s), int((t - int(t)) * 1000))


def main():
	dossier, sortie = sys.argv[1], sys.argv[2]
	debut_webm = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
	langue_salle = os.environ.get("CEFFIO_LANGUE", "")

	pistes = {}
	chemin = os.path.join(dossier, "pistes.json")
	if os.path.exists(chemin):
		pistes = json.load(open(chemin))

	noms = {}
	chemin_noms = os.path.join(dossier, "noms.json")
	if os.path.exists(chemin_noms):
		noms = json.load(open(chemin_noms))

	from faster_whisper import WhisperModel

	modele = WhisperModel(MODELE, device="cpu", compute_type="int8",
		cpu_threads=THREADS, download_root=MODELES)

	lignes = []
	for wav in sorted(glob.glob(os.path.join(dossier, "*.wav"))):
		session = os.path.basename(wav)[:-4]
		qui = noms.get(session, "participant " + session[:6])
		try:
			encoder_opus(wav, wav[:-4] + ".opus")
		except Exception as e:
			print("opus ignoré %s : %r" % (session[:10], e), flush=True)
		reference = pistes.get(session, debut_webm)
		for seg in transcrire(wav, langue_salle, modele):
			debut = seg["debut"] + reference - debut_webm
			fin = seg["fin"] + reference - debut_webm
			if fin <= 0:
				continue
			lignes.append({"debut": max(0.0, debut), "fin": fin, "qui": qui, "texte": seg["texte"]})

	lignes.sort(key=lambda x: x["debut"])
	with open(sortie, "w", encoding="utf-8") as f:
		for i, l in enumerate(lignes, 1):
			f.write("%d\n%s --> %s\n%s : %s\n\n" % (i, horodate(l["debut"]), horodate(l["fin"]), l["qui"], l["texte"]))
	print(json.dumps({"segments": len(lignes), "sortie": sortie}), flush=True)


if __name__ == "__main__":
	main()
