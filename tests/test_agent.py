"""The endpoint and the worker, without a cluster.

THE KUBERNETES CLIENT IS STUBBED BEFORE agent IS IMPORTED: what is tested here
is the contract the relay depends on — 202 for an authentic notification, 403
for a forged one, and the effects carried out once — not the API calls the pod
will make. Those are exercised on a live installation.
"""
import http.client
import json
import os
import queue
import sys
import threading
import types
import unittest
from http.server import HTTPServer

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, APP)
os.environ["CEFFIO_RELAY_SECRET"] = "s3cret"

# Stub the client before the agent imports it: `from kubernetes import client,
# config, stream` needs the three names to exist, and nothing more at import.
for _nom in ("kubernetes", "kubernetes.client", "kubernetes.config", "kubernetes.stream"):
	sys.modules.setdefault(_nom, types.ModuleType(_nom))
_kubernetes = sys.modules["kubernetes"]
_kubernetes.client = sys.modules["kubernetes.client"]
_kubernetes.config = sys.modules["kubernetes.config"]
_kubernetes.stream = sys.modules["kubernetes.stream"]

import agent  # noqa: E402
import notification  # noqa: E402

SECRET = "s3cret"


def corps(type_="start", token="salon1"):
	return json.dumps({"type": type_, "token": token, "owner": "alice",
		"actor": {"type": "users", "id": "alice"},
		"backend": "https://cloud.exemple.org/",
		"at": "2026-10-26T20:00:00Z"}).encode()


class Serveur:
	"""The agent's HTTP handler on a free port, in a thread."""

	def __init__(self):
		self.srv = HTTPServer(("127.0.0.1", 0), agent.Sante)
		self.port = self.srv.server_address[1]
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()

	def poster(self, chemin, corps, aleatoire="abc123", somme=None, entetes=None):
		connexion = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
		entetes = dict(entetes or {})
		entetes["Content-Type"] = "application/json"
		if aleatoire:
			entetes["Ceffio-Relay-Random"] = aleatoire
			entetes["Ceffio-Relay-Checksum"] = somme or notification.signature(SECRET, aleatoire, corps)
		connexion.request("POST", chemin, body=corps, headers=entetes)
		rep = connexion.getresponse()
		return rep.status, rep.read()

	def fermer(self):
		self.srv.shutdown()


def vider_file():
	while True:
		try:
			agent._FILE.get_nowait()
			agent._FILE.task_done()
		except queue.Empty:
			return


class TestEndpoint(unittest.TestCase):
	def setUp(self):
		vider_file()
		self.serveur = Serveur()

	def tearDown(self):
		self.serveur.fermer()

	def test_notification_authentique_acceptee(self):
		code, corps_rep = self.serveur.poster("/v1/recording", corps())
		# 202, PAS 200: le travail est mis en file, pas fait — le relais n'attend
		# que deux secondes et ne réessaie pas.
		self.assertEqual(code, 202, corps_rep)
		self.assertEqual(agent._FILE.get_nowait()["token"], "salon1")

	def test_signature_fausse_refusee(self):
		code, _ = self.serveur.poster("/v1/recording", corps(), somme="0" * 64)
		self.assertEqual(code, 403)
		with self.assertRaises(queue.Empty):
			agent._FILE.get_nowait()

	def test_signature_absente_refusee(self):
		code, _ = self.serveur.poster("/v1/recording", corps(), aleatoire="")
		self.assertEqual(code, 403)

	def test_corps_falsifie_refuse(self):
		code, _ = self.serveur.poster("/v1/recording", corps().replace(b"start", b"stop"),
			somme=notification.signature(SECRET, "abc123", corps()))
		self.assertEqual(code, 403)

	def test_type_inconnu_refuse_apres_la_signature(self):
		code, _ = self.serveur.poster("/v1/recording", corps("pause"))
		self.assertEqual(code, 400)

	def test_chemin_inconnu(self):
		code, _ = self.serveur.poster("/autre", corps())
		self.assertEqual(code, 404)

	def test_sans_corps(self):
		code, _ = self.serveur.poster("/v1/recording", b"")
		self.assertEqual(code, 400)

	def test_corps_trop_gros(self):
		code, _ = self.serveur.poster("/v1/recording", b"a" * (notification.MAX_BODY + 1))
		self.assertEqual(code, 413)


class TestTravail(unittest.TestCase):
	"""The worker's wiring: each authentic notification has one effect."""

	def setUp(self):
		self.appels = []
		self._vrais = (agent.demarrer, agent.arreter, agent.finaliser, agent.nettoyer)
		agent.demarrer = lambda jeton: self.appels.append(("demarrer", jeton))
		agent.arreter = lambda jeton: self.appels.append(("arreter", jeton)) or {"jeton": jeton}
		agent.finaliser = lambda jeton, etat: self.appels.append(("finaliser", jeton))
		agent.nettoyer = lambda jeton: self.appels.append(("nettoyer", jeton))
		agent._etat = notification.Etat()

	def tearDown(self):
		agent.demarrer, agent.arreter, agent.finaliser, agent.nettoyer = self._vrais

	def avis(self, type_, token="salon1"):
		return {"type": type_, "token": token, "at": "2026-10-26T20:00:00Z"}

	def test_start_puis_stop(self):
		agent.servir_un(self.avis("start"))
		agent.servir_un(self.avis("stop"))
		self.assertEqual(self.appels, [
			("demarrer", "salon1"), ("arreter", "salon1"),
			("finaliser", "salon1"), ("nettoyer", "salon1")])

	def test_start_en_double_sans_effet(self):
		agent.servir_un(self.avis("start"))
		agent.servir_un(self.avis("start"))
		self.assertEqual(self.appels, [("demarrer", "salon1")])
		self.assertIn("ignored", agent._derniere_notification["outcome"])

	def test_stop_sans_start_sans_effet(self):
		agent.servir_un(self.avis("stop"))
		self.assertEqual(self.appels, [])
		self.assertIn("ignored", agent._derniere_notification["outcome"])

	def test_une_deuxieme_capture_apres_le_stop(self):
		agent.servir_un(self.avis("start"))
		agent.servir_un(self.avis("stop"))
		agent.servir_un(self.avis("start"))
		self.assertEqual([a for a in self.appels if a[0] == "demarrer"],
			[("demarrer", "salon1"), ("demarrer", "salon1")])


if __name__ == "__main__":
	unittest.main()
