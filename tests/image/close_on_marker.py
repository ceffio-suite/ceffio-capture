"""Prouve, DANS L'IMAGE, que les pistes sont écrites quand le marqueur disparaît.

CE FICHIER N'EST PAS DANS LA CI : il a besoin de PyAV et d'aiortc, donc de
l'image. Il se lance dedans, avant de demander à quiconque de refaire un
enregistrement :

    kubectl -n ceffio-video cp tests/image/close_on_marker.py <pod>:/tmp/x.py -c capture
    kubectl -n ceffio-video exec <pod> -c capture -- python3 /tmp/x.py

Ce qu'il vérifie, et pourquoi c'est le bon test : le défaut réel n'était pas la
souscription — elle avait produit 112 Ko de pistes sur un vrai appel — mais la
FERMETURE. PyAV n'écrit qu'au close(), et le processus était tué par SIGTERM
avant. Ici, le vrai main() de capture.py tourne avec un faux signaling qui émet
de l'audio, et le marqueur est retiré comme le fait l'agent.

Mesuré le 28/10 dans l'image v0.8.2 : deux pistes, 13 184 octets, 93 trames
décodables — contre 0 octet avant la correction.
"""
import asyncio
import os
import shutil
import sys

TRAVAIL = "/tmp/essai-capture"
shutil.rmtree(TRAVAIL, ignore_errors=True)
os.makedirs(os.path.join(TRAVAIL, "pistes"), exist_ok=True)
open(os.path.join(TRAVAIL, "recording"), "w").close()

os.environ["CEFFIO_ROOM"] = "essai"
os.environ["CEFFIO_WORK"] = TRAVAIL
os.environ["CEFFIO_HPB_URL"] = "wss://exemple.invalid/spreed"
os.environ["CEFFIO_NEXTCLOUD_URL"] = "https://exemple.invalid"
os.environ["CEFFIO_INTERNAL_SECRET"] = "x"

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/lib")

import av  # noqa: E402
import numpy as np  # noqa: E402
from livetypes import SigConnectResult  # noqa: E402
import capture  # noqa: E402


class Flux:
	"""Une trame de 20 ms toutes les 20 ms : 1 seconde = 50 trames."""

	def __init__(self, sid):
		self.sid = sid
		self.n = 0

	async def receive(self):
		await asyncio.sleep(0.02)
		self.n += 1
		if self.n > 200:  # 4 s, puis le flux se tait (fenêtre morte)
			await asyncio.sleep(3600)
		t = np.arange(960, dtype=np.float32) / 48000.0
		onde = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
		f = av.AudioFrame.from_ndarray(onde.reshape(1, -1), format="s16", layout="mono")
		f.sample_rate = 48000
		return f


class FauxClient:
	def __init__(self, *a, **k):
		self.nc_sid_map = {"session-a": "nc-a", "session-b": "nc-b"}
		self.roster = {}
		self.defunct = asyncio.Event()

	async def connect(self):
		return SigConnectResult.SUCCESS

	async def add_target(self, nc_sid):
		sid = "session-a" if nc_sid == "nc-a" else "session-b"
		piste = capture.PisteOpus(sid)
		await piste.start(Flux(sid))

	async def close(self):
		self.defunct.set()


capture.SpreedClient = FauxClient


async def essai():
	tache = asyncio.create_task(capture.main())
	await asyncio.sleep(1.5)  # ~75 trames par piste
	print("marqueur retiré (l'agent fait exactement ça)", flush=True)
	os.remove(os.path.join(TRAVAIL, "recording"))
	attendre = asyncio.ensure_future(tache)
	await asyncio.wait_for(attendre, timeout=10)
	return attendre.result()


asyncio.run(essai())

pistes = sorted(p for p in os.listdir(os.path.join(TRAVAIL, "pistes")) if p.endswith(".opus"))
print("pistes :", len(pistes))
ok = True
for nom in pistes:
	chemin = os.path.join(TRAVAIL, "pistes", nom)
	taille = os.path.getsize(chemin)
	try:
		with av.open(chemin) as c:
			trames = sum(1 for _ in c.decode(c.streams.audio[0]))
	except Exception as e:
		trames = -1
		print("illisible :", repr(e))
	print("%s : %d octets, %d trames" % (nom[:14], taille, trames))
	ok = ok and taille > 1000 and trames > 0
print("RESULTAT :", "les pistes sont écrites et lisibles" if ok else "ECHEC")
sys.exit(0 if ok else 1)
