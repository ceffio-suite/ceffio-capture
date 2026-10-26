"""Capture client: joins a Talk call and writes ONE TRACK PER PARTICIPANT.

Signaling and WebRTC negotiation come from the transcription app's client
(lib/spreed_client.py, vendored and patched): that path is proven.
Seule la destination de l'audio change — un fichier WAV par participant, au
lieu du transcripteur en direct.

Variables d'environnement :
  CEFFIO_ROOM              jeton de la salle
  CEFFIO_HPB_URL           wss://spreed.<domaine>/spreed
  CEFFIO_NEXTCLOUD_URL     https://cloud.<domaine>
  CEFFIO_INTERNAL_SECRET   shared signaling secret
  CEFFIO_WORK              working directory (default /work)
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

import av  # noqa: E402
import spreed_client  # noqa: E402
from livetypes import HPBSettings, SigConnectResult  # noqa: E402
from spreed_client import SpreedClient  # noqa: E402

TRAVAIL = os.environ.get("CEFFIO_WORK", "/work")
SORTIE = os.path.join(TRAVAIL, "pistes")

# WRITE NOTHING UNTIL THE RECORDING STARTS. The client joins the call as soon
# as it begins — that is what gives it time to subscribe before anyone presses
# record — but a meeting nobody records must not leave audio on disk. The agent
# creates this marker when the recording starts, and the track opens on the
# first frame seen after it appears.
MARQUEUR = os.path.join(TRAVAIL, "recording")


class PisteOpus:
	"""Stand-in for the app's transcriber: encodes each track to Opus.

	NO INTERMEDIATE WAV: the WebRTC stream is already Opus; it is re-encoded at
	a neighbouring bitrate (32 kbit/s mono) and that file is what transcription
	reads back. A WAV would only multiply disk usage by ten for a format nobody
	delivers.
	"""

	def __init__(self, sid, *args, **kwargs):
		self.sid = sid
		self.conteneur = None
		self.piste = None
		self.tache = None
		# THE ANCHOR IS THE FIRST SAMPLE, NOT THE TRACK OPENING.
		#
		# The track object exists as soon as the offer is accepted, but the
		# first audio sample only arrives once ICE and DTLS are done — seconds
		# later, and a different number of seconds for each participant. Dating
		# the track at its opening made every transcript start too early by
		# exactly that delay, which is why the first twenty seconds of a live
		# recording went missing and the rest drifted.
		#
		# All pods on a node share the kernel clock, so this timestamp and the
		# recorder's file name are on the same time base: no drift to correct,
		# only the real delay to measure.
		self.ouvert = None
		self.resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)

	async def connect(self):
		return None

	def _ouvrir(self):
		chemin = os.path.join(SORTIE, f"{self.sid}.opus")
		self.conteneur = av.open(chemin, "w", format="ogg")
		self.piste = self.conteneur.add_stream("libopus", rate=48000)
		self.piste.layout = "mono"
		self.piste.bit_rate = 32000
		return chemin

	async def start(self, stream):
		# The container is opened on the first frame seen AFTER the recording
		# marker appears; until then the client is subscribed and discards.
		self.tache = asyncio.create_task(self._boucle(stream), name=f"piste-{self.sid}")

	def _dater(self, frame):
		"""Stamp the track with the arrival of its FIRST written sample."""
		duree = float(getattr(frame, "samples", 0)) / float(getattr(frame, "sample_rate", 48000) or 48000)
		self.ouvert = time.time() - duree
		journal = os.path.join(SORTIE, "pistes.json")
		existantes = {}
		if os.path.exists(journal):
			try:
				existantes = json.load(open(journal))
			except Exception:
				existantes = {}
		existantes[self.sid] = self.ouvert
		with open(journal, "w") as f:
			json.dump(existantes, f)

	async def _boucle(self, stream):
		try:
			while True:
				frame = await stream.receive()
				if self.conteneur is None:
					if not os.path.exists(MARQUEUR):
						continue
					chemin = self._ouvrir()
					self._dater(frame)
					print(f"piste ouverte {self.sid[:10]} -> {chemin}", flush=True)
				for sortie in self.resampler.resample(frame):
					sortie.pts = None
					for paquet in self.piste.encode(sortie):
						self.conteneur.mux(paquet)
		except Exception as e:
			print(f"fin de piste {self.sid[:10]} : {e!r}", flush=True)
		finally:
			self._fermer()

	def _fermer(self):
		if self.conteneur is not None:
			try:
				for paquet in self.piste.encode(None):
					self.conteneur.mux(paquet)
				self.conteneur.close()
			except Exception:
				pass
			self.conteneur = None

	async def shutdown(self):
		if self.tache is not None:
			self.tache.cancel()
		self._fermer()


spreed_client.VoskTranscriber = PisteOpus



async def main():
	room = os.environ["CEFFIO_ROOM"]
	os.makedirs(SORTIE, exist_ok=True)

	hpb = HPBSettings(server=os.environ["CEFFIO_HPB_URL"], stunservers=[], turnservers=[])
	client = SpreedClient(room, hpb, "en", None)

	# The transcription app leaves a call with no receiver; a capture must stay
	# as long as the call lives (the end of the call always closes the client).
	async def rester():
		while True:
			await asyncio.sleep(3600)

	client.maybe_leave_call = rester

	resultat = await client.connect()
	print(f"connect() = {resultat}", flush=True)
	if resultat != SigConnectResult.SUCCESS:
		raise SystemExit("connexion au signaling impossible")

	ajoutes = set()
	try:
		while not client.defunct.is_set():
			for nc_sid in list(client.nc_sid_map):
				if nc_sid in ajoutes:
					continue
				await client.add_target(nc_sid)
				ajoutes.add(nc_sid)
			with open(os.path.join(SORTIE, "participants.json"), "w") as f:
				json.dump(getattr(client, "roster", {}), f, indent=2, ensure_ascii=False)
			await asyncio.sleep(2)
	finally:
		await client.close()


if __name__ == "__main__":
	asyncio.run(main())
