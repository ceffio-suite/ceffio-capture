"""Client de capture : rejoint un appel Talk et écrit UNE PISTE PAR PARTICIPANT.

Le signaling et la négociation WebRTC viennent du client de l'app de
transcription (lib/spreed_client.py, vendé et patché) : ce chemin est éprouvé.
Seule la destination de l'audio change — un fichier WAV par participant, au
lieu du transcripteur en direct.

Variables d'environnement :
  CEFFIO_ROOM              jeton de la salle
  CEFFIO_HPB_URL           wss://spreed.<domaine>/spreed
  CEFFIO_NEXTCLOUD_URL     https://cloud.<domaine>
  CEFFIO_INTERNAL_SECRET   secret partagé du signaling
  CEFFIO_WORK              répertoire de travail (défaut /work)
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


class PisteOpus:
	"""Remplace le transcripteur de l'app : encode chaque piste en Opus.

	PAS DE WAV INTERMÉDIAIRE : le flux WebRTC est en Opus, on le ré-encode à un
	débit voisin (32 kbit/s mono) et c'est ce fichier que la transcription
	relit. Un WAV ne ferait que multiplier le disque par dix pour un format que
	personne ne livre.
	"""

	def __init__(self, sid, *args, **kwargs):
		self.sid = sid
		self.conteneur = None
		self.piste = None
		self.tache = None
		self.ouvert = time.time()
		self.resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)

	async def connect(self):
		return None

	async def start(self, stream):
		chemin = os.path.join(SORTIE, f"{self.sid}.opus")
		self.conteneur = av.open(chemin, "w", format="ogg")
		self.piste = self.conteneur.add_stream("libopus", rate=48000)
		self.piste.layout = "mono"
		self.piste.bit_rate = 32000

		# L'instant d'ouverture de la piste : l'abonnement WebRTC n'arrive pas
		# au début de l'enregistrement, et le délai varie par participant.
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
		print(f"piste ouverte {self.sid[:10]} -> {chemin}", flush=True)
		self.tache = asyncio.create_task(self._boucle(stream), name=f"piste-{self.sid}")

	async def _boucle(self, stream):
		try:
			while True:
				frame = await stream.receive()
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

	# L'app de transcription quitte l'appel sans récepteur ; une capture doit
	# rester tant que l'appel vit (la fin d'appel ferme toujours le client).
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
