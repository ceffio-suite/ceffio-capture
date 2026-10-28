"""The endpoint and the worker, without a cluster.

THE KUBERNETES CLIENT IS STUBBED BEFORE agent IS IMPORTED: what is tested here
is the contract the relay depends on — 202 for an authentic notification, 403
for a forged one, and the effects carried out once — not the API calls the pod
will make. Those are exercised on a live installation.
"""
import base64
import http.client
import subprocess
import tempfile
import json
import os
import queue
import sys
import threading
import time
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
	# THE TIMESTAMP MUST BE INSIDE THE REPLAY WINDOW: a value frozen in the past
	# would make the notification refused two minutes into the test's life —
	# which is exactly what the window is for.
	return json.dumps({"type": type_, "token": token, "owner": "alice",
		"actor": {"type": "users", "id": "alice"},
		"backend": "https://cloud.exemple.org/",
		"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()


class Serveur:
	"""The agent's HTTP handler on a free port, in a thread."""

	def __init__(self):
		self.srv = HTTPServer(("127.0.0.1", 0), agent.Sante)
		self.port = self.srv.server_address[1]
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()

	def poster(self, chemin, corps, aleatoire=None, somme=None, entetes=None):
		# A FRESH NONCE BY DEFAULT: the replay window refuses the same one twice,
		# and a test reusing "abc123" everywhere would be refused from the second
		# send on.
		aleatoire = os.urandom(8).hex() if aleatoire is None else aleatoire
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
		# two seconds and does not retry.
		self.assertEqual(code, 202, corps_rep)
		self.assertEqual(agent._FILE.get_nowait()["token"], "salon1")

	def test_signature_fausse_refusee(self):
		code, _ = self.serveur.poster("/v1/recording", corps(), somme="0" * 64)
		self.assertEqual(code, 403)
		with self.assertRaises(queue.Empty):
			agent._FILE.get_nowait()

	def test_rejeu_de_la_meme_notification_refuse(self):
		corps_ = corps()
		nonce = "nonce-de-test-1"
		code, _ = self.serveur.poster("/v1/recording", corps_, aleatoire=nonce)
		self.assertEqual(code, 202)
		# MÊME CORPS, MÊME NONCE : la signature est valide, et c'est justement le
		# case the window must stop — otherwise a captured notification would
		# open a second job for the same room.
		code, _ = self.serveur.poster("/v1/recording", corps_, aleatoire=nonce)
		self.assertEqual(code, 400)
		vider_file()

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


class TestSansWebm(unittest.TestCase):
	"""A stop with no .webm must be a FAILED job, not a silent one."""

	def test_le_statut_dit_echec(self):
		agent._dernier.update({"result": "none", "error": None})
		vrai = agent.attendre_webm
		agent.attendre_webm = lambda jeton, avant, delai=0: ""
		try:
			agent.finaliser("salon1", {"webm_avant": set(), "langue": ""})
		finally:
			agent.attendre_webm = vrai
		self.assertEqual(agent._dernier["result"], "failed")
		self.assertIn("webm", agent._dernier["error"])


class TestCitationDesChemins(unittest.TestCase):
	"""Le chemin du .srt porte l'identifiant Nextcloud, qui accepte l'apostrophe.

	The test runs the REAL command, with a real `sh`, in a directory whose name
	contains "o'brien x": without quoting, the file is not written where we look
	— exactly what would happen in the pod.
	"""

	def test_un_identifiant_avec_apostrophe_et_espace(self):
		tmp = tempfile.mkdtemp()
		# THE DIRECTORY NEXTCLOUD CREATES FOR SUCH A USER.
		dossier = os.path.join(tmp, "o'brien x", "files", "Talk", "Recording", "tok1234")
		os.makedirs(dossier)
		chemin_srt = os.path.join(dossier, "Recording 2026-10-28 10-00-00.srt")
		contenu = "1\n00:00:00,200 --> 00:00:01,000\namina : bonjour\n"
		b64 = base64.b64encode(contenu.encode()).decode()

		subprocess.run(agent.commande_ecriture(chemin_srt, b64), check=True)

		self.assertTrue(os.path.exists(chemin_srt), "the .srt was not written: quoting is missing")
		with open(chemin_srt, encoding="utf-8") as f:
			self.assertEqual(f.read(), contenu)

	def test_une_injection_ne_s_execute_pas(self):
		# THE TOKEN IS REFUSED UPSTREAM; this is the second barrier: even a path
		# containing a semicolon must stay a FILE NAME, not a command. It is run
		# to prove it.
		tmp = tempfile.mkdtemp()
		piege = os.path.join(tmp, "x; touch pirate_injecte ;.srt")
		subprocess.run(agent.commande_ecriture(piege, "YWJj"), check=True, cwd=tmp)
		self.assertFalse(os.path.exists(os.path.join(tmp, "pirate_injecte")),
			"the semicolon was interpreted: the value is not quoted")
		self.assertTrue(os.path.exists(piege), "le fichier au nom bizarre doit exister, lui")


if __name__ == "__main__":
	unittest.main()


class TestCheminDeScan(unittest.TestCase):
	"""The path `occ files:scan` receives.

	THIS BUG COST A REAL RECORDING: a local `dossier = ...` shadowed the
	`dossier()` function, and the deposit raised UnboundLocalError after a
	successful transcription. The test covers the path conversion, the one part
	that can be wrong without showing in the logs.
	"""

	def test_chemin_relatif_a_la_racine_des_donnees(self):
		self.assertEqual(
			agent.chemin_scan("/var/www/html/data/amina/files/Talk/Recording/tok/Recording 2026-10-27 11-50-17.webm"),
			"amina/files/Talk/Recording/tok")

	def test_chemin_hors_racine_rendu_tel_quel(self):
		# An unexpected path must not be truncated at random: a scan that fails
		# loudly is better than a path pointing at another directory.
		self.assertEqual(agent.chemin_scan("/autre/endroit/x.webm"), "/autre/endroit")


class TestArretPropre(unittest.TestCase):
	"""The client must be given time to CLOSE its files.

	PyAV writes the audio only at close(): killing the process first leaves a
	zero-byte track, which is what a real recording produced before this test
	existed. These two tests use a real subprocess, because the property is
	about process control, not about code paths.
	"""

	SCRIPT = "import os,sys,time\nwhile time.time() < %d:\n time.sleep(0.05)\n"

	def _capture(self, obeissant):
		# LE MARQUEUR DOIT ÊTRE CELUI QUE arreter() SUPPRIME : il est construit
		# par dossier(jeton), donc sous TRAVAIL/salons/<jeton>/recording.
		tmp = tempfile.mkdtemp()
		self.vrai_travail = agent.TRAVAIL
		agent.TRAVAIL = tmp
		os.makedirs(os.path.join(tmp, "salons", "salon1"))
		marqueur = os.path.join(tmp, "salons", "salon1", "recording")
		open(marqueur, "w").close()
		# THE PROCESS LOOKS AT THE MARKER, exactly like the real client: it exits
		# when the file disappears, and ignores it when told to.
		code = (
			"import os,time,sys\n"
			"m = %r\n"
			"fin = time.time() + 20\n"
			"while time.time() < fin:\n"
			"    if %r and not os.path.exists(m):\n"
			"        open(%r, 'w').write('ferme')\n"
			"        sys.exit(0)\n"
			"    time.sleep(0.05)\n"
		) % (marqueur, obeissant, os.path.join(tmp, "ferme"))
		proc = subprocess.Popen([sys.executable, "-c", code])
		agent._captures["salon1"] = {"proc": proc, "debut": time.time(),
			"webm_avant": set(), "langue": "", "recording": True}
		return tmp, marqueur, os.path.join(tmp, "ferme"), proc

	def setUp(self):
		self.vrai = agent.DELAI_ARRET
		agent.DELAI_ARRET = 1.0

	def tearDown(self):
		agent.DELAI_ARRET = self.vrai
		if getattr(self, "vrai_travail", None):
			agent.TRAVAIL = self.vrai_travail
		agent._captures.clear()

	def test_le_client_ferme_ses_fichiers_avant_le_signal(self):
		tmp, marqueur, ferme, proc = self._capture(obeissant=True)
		agent.arreter("salon1")
		self.assertTrue(os.path.exists(ferme), "le client n'a pas eu le temps de fermer : "
			"the audio would have stayed in PyAV's buffer")
		self.assertEqual(proc.returncode, 0)

	def test_un_client_bloque_est_fini_par_le_signal(self):
		tmp, marqueur, ferme, proc = self._capture(obeissant=False)
		agent.arreter("salon1")
		self.assertFalse(os.path.exists(ferme))
		self.assertIsNotNone(proc.returncode, "the stuck process must be killed")
