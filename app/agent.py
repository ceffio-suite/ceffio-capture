"""Agent du service de capture.

Boucle : surveille l'état d'enregistrement des salles Talk, lance la capture
quand un enregistrement démarre, l'arrête avec lui, transcrit, puis dépose les
fichiers à côté du .webm — dans les fichiers de celui qui a lancé
l'enregistrement, puisque c'est là que Talk range le .webm.

La lecture de l'état et le dépôt passent par l'API Kubernetes (exec dans les
pods Nextcloud et PostgreSQL) : le service n'a besoin ni d'identifiant de base
de données, ni d'accès au volume Nextcloud.
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from kubernetes import client, config, stream

NS_FILES = os.environ.get("CEFFIO_NC_NAMESPACE", "ceffio-files")
NS_DATA = os.environ.get("CEFFIO_DB_NAMESPACE", "ceffio-data")
NS_VIDEO = os.environ.get("CEFFIO_NAMESPACE", "ceffio-video")
TRAVAIL = os.environ.get("CEFFIO_WORK", "/work")
PISTES = os.path.join(TRAVAIL, "pistes")
LANGUE_DEFAUT = os.environ.get("CEFFIO_LANGUE", "")
INTERVALLE = float(os.environ.get("CEFFIO_INTERVALLE", "1"))

_api = None
_captures = {}   # token -> {"proc": Popen, "debut": epoch, "webm_avant": set, "langue": str}


def api():
	global _api
	if _api is None:
		try:
			config.load_incluster_config()
		except Exception:
			config.load_kube_config()
		_api = client.CoreV1Api()
	return _api


def pod_de(namespace, etiquette):
	pods = api().list_namespaced_pod(namespace, label_selector=etiquette).items
	for p in pods:
		if p.status.phase == "Running":
			return p.metadata.name
	raise RuntimeError(f"aucun pod {etiquette} en service dans {namespace}")


def executer(namespace, pod, commande, entree=None, conteneur=None):
	"""Lance une commande dans un pod et rend sa sortie."""
	resp = stream.stream(
		api().connect_get_namespaced_pod_exec, pod, namespace,
		container=conteneur, command=commande, stderr=True, stdin=entree is not None,
		stdout=True, tty=False, _preload_content=False,
	)
	sortie = ""
	if entree is not None:
		resp.write_stdin(entree)
	while resp.is_open():
		resp.update(timeout=1)
		if resp.peek_stdout():
			sortie += resp.read_stdout()
		if resp.peek_stderr():
			print(resp.read_stderr(), flush=True)
	resp.close()
	return sortie


def psql(requete):
	pod = pod_de(NS_DATA, "cnpg.io/cluster=ceffio-pg,cnpg.io/instanceRole=primary")
	return executer(NS_DATA, pod, ["psql", "-U", "postgres", "-d", "nextcloud", "-At", "-F", "|", "-c", requete])


def salons_enregistres():
	"""[(jeton, état, langue)] des salles en cours d'enregistrement."""
	lignes = psql("select token, call_recording, coalesce(transcription_language,'') from oc_talk_rooms where call_recording <> 0")
	resultat = []
	for ligne in lignes.splitlines():
		parts = ligne.split("|")
		if len(parts) == 3 and parts[0]:
			resultat.append((parts[0], int(parts[1]), parts[2]))
	return resultat


def webm_de_la_salle(jeton):
	pod = pod_de(NS_FILES, "app=nextcloud")
	return executer(NS_FILES, pod, ["sh", "-c",
		f"ls -1 /var/www/html/data/*/files/Talk/Recording/{jeton}/*.webm 2>/dev/null"]).split()


def demarrer(jeton, langue):
	os.makedirs(PISTES, exist_ok=True)
	environnement = dict(os.environ, CEFFIO_ROOM=jeton, CEFFIO_WORK=TRAVAIL)
	log = open(os.path.join(TRAVAIL, f"capture-{jeton}.log"), "w")
	proc = subprocess.Popen(["python3", "/app/capture.py"], env=environnement, stdout=log, stderr=subprocess.STDOUT)
	_captures[jeton] = {"proc": proc, "debut": time.time(), "webm_avant": set(webm_de_la_salle(jeton)), "langue": langue}
	print(f"capture lancée pour {jeton} (langue {langue or 'auto'})", flush=True)


def arreter(jeton):
	etat = _captures.pop(jeton, None)
	if not etat:
		return
	etat["proc"].terminate()
	try:
		etat["proc"].wait(timeout=15)
	except subprocess.TimeoutExpired:
		etat["proc"].kill()
	return etat


def nouveaux_webm(jeton, avant):
	for chemin in webm_de_la_salle(jeton):
		if chemin not in avant and os.path.basename(chemin) != "":
			return chemin
	return ""


def noms(jeton, sessions):
	"""Session HPB -> nom, par la base : sessions Nextcloud, puis participants."""
	if not sessions:
		return {}
	liste = ",".join("'%s'" % s.replace("'", "") for s in sessions)
	requete = ("select s.session_id, a.display_name from oc_talk_sessions s "
		f"join oc_talk_attendees a on a.id = s.attendee_id where s.session_id in ({liste})")
	noms = {}
	for ligne in psql(requete).splitlines():
		if "|" in ligne:
			session, nom = ligne.split("|", 1)
			noms[session] = nom
	return noms


def deposer(jeton, etat, chemin_webm):
	base = chemin_webm[:-5]
	pod = pod_de(NS_FILES, "app=nextcloud")

	def ecrire(distant, donnees):
		executer(NS_FILES, pod, ["sh", "-c", f"cat > '{distant}'"], entree=donnees)

	srt = os.path.join(TRAVAIL, "transcription.srt")
	with open(srt, "rb") as f:
		ecrire(base + ".srt", f.read())

	for opus in sorted(p.name for p in os.scandir(PISTES) if p.name.endswith(".opus")):
		with open(os.path.join(PISTES, opus), "rb") as f:
			ecrire(f"{base}.{opus[:-5][:10]}.opus", f.read())

	# Les sous-titres DANS le webm : « Partager dans la conversation » partage
	# alors un fichier qui contient déjà sa transcription (copie de flux).
	local = os.path.join(TRAVAIL, "webm-source.webm")
	with open(local, "wb") as f:
		f.write(executer(NS_FILES, pod, ["cat", chemin_webm]).encode("latin-1", "ignore"))
	muet = os.path.join(TRAVAIL, "webm-sous-titres.webm")
	copie = subprocess.run(["ffmpeg", "-y", "-i", local, "-i", srt, "-c", "copy", "-c:s", "webvtt",
		"-disposition:s:0", "default", muet], capture_output=True)
	if copie.returncode == 0 and os.path.getsize(muet) > 0:
		with open(muet, "rb") as f:
			ecrire(chemin_webm, f.read())

	dossier = os.path.dirname(chemin_webm)
	executer(NS_FILES, pod, ["sh", "-c", f"chown www-data:www-data '{base}'* 2>/dev/null; true"])
	executer(NS_FILES, pod, ["su", "-s", "/bin/sh", "www-data", "-c",
		f"php occ files:scan --path=\"{dossier[len('/var/www/html/data/'):]}\""])
	print(f"déposé {base}.srt", flush=True)


def finaliser(jeton, etat):
	webm = nouveaux_webm(jeton, etat["webm_avant"])
	if not webm:
		print(f"aucun .webm nouveau pour {jeton} : transcription non déposée", flush=True)
		return
	nom = os.path.basename(webm)[: -len(".webm")].replace("Recording ", "").replace(" ", "T")
	import calendar

	debut_webm = float(calendar.timegm(time.strptime(nom + "Z", "%Y-%m-%dT%H-%M-%SZ")))

	sessions = [os.path.basename(p)[:-4] for p in os.listdir(PISTES) if p.endswith(".wav")]
	with open(os.path.join(PISTES, "noms.json"), "w") as f:
		json.dump(noms(jeton, sessions), f)

	environnement = dict(os.environ, CEFFIO_LANGUE=etat.get("langue", ""))
	subprocess.run(["python3", "/app/finaliser.py", PISTES, os.path.join(TRAVAIL, "transcription.srt"), str(debut_webm)],
		env=environnement, check=False)
	deposer(jeton, etat, webm)


def boucle():
	while True:
		try:
			actifs = {jeton: langue for jeton, _, langue in salons_enregistres()}
			for jeton in list(_captures):
				if jeton not in actifs:
					etat = arreter(jeton)
					finaliser(jeton, etat)
			for jeton, langue in actifs.items():
				if jeton not in _captures:
					demarrer(jeton, langue or LANGUE_DEFAUT)
		except Exception as e:
			print(f"boucle : {e!r}", flush=True)
		time.sleep(INTERVALLE)


class Sante(BaseHTTPRequestHandler):
	def do_GET(self):
		self.send_response(200 if self.path == "/healthz" else 404)
		self.end_headers()
		self.wfile.write(b"ok")

	def log_message(self, *args):
		pass


if __name__ == "__main__":
	os.makedirs(PISTES, exist_ok=True)
	threading.Thread(target=boucle, daemon=True).start()
	print("agent de capture démarré", flush=True)
	HTTPServer(("", 8080), Sante).serve_forever()
