"""Agent du service de capture.

Boucle : surveille l'état d'enregistrement des salles Talk, lance la capture
quand un enregistrement démarre, l'arrête avec lui, transcrit, puis dépose les
fichiers à côté du .webm — dans les fichiers de celui qui a lancé
l'enregistrement, puisque c'est là que Talk range le .webm.

La lecture de l'état et le dépôt passent par l'API Kubernetes (exec dans les
pods Nextcloud et PostgreSQL) : le service n'a besoin ni d'identifiant de base
de données, ni d'accès au volume Nextcloud.
"""
import base64
import glob
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
		# EOF on the stdin channel: without it the remote `cat > file` never
		# sees the end of its input and the call hangs until the timeout —
		# observed on the first live deposit.
		if hasattr(resp, "close_stdin"):
			resp.close_stdin()
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


def rooms_in_call():
	"""Rooms where a call is running — the capture must join BEFORE recording.

	WHY: the recording starts first, and the WebRTC subscription takes tens of
	seconds to establish (ICE, then the offer per publisher). Starting the
	capture on the recording left the first 20 seconds of a live test missing.
	A call always starts before anyone records, so that is when we join.
	"""
	lines = psql("select distinct r.token from oc_talk_sessions s "
		"join oc_talk_attendees a on a.id = s.attendee_id "
		"join oc_talk_rooms r on r.id = a.room_id where s.in_call > 0")
	return [l.strip() for l in lines.splitlines() if l.strip()]


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
	# splitlines(), NOT split(): a recording path contains spaces
	# ("Recording 2026-10-25 23-00-52.webm"), and splitting on whitespace
	# returned fragments that parse as a time and fail.
	return [ligne.strip() for ligne in executer(NS_FILES, pod, ["sh", "-c",
		f"ls -1 /var/www/html/data/*/files/Talk/Recording/{jeton}/*.webm 2>/dev/null"]).splitlines() if ligne.strip()]


def demarrer(jeton, langue):
	os.makedirs(PISTES, exist_ok=True)
	environnement = dict(os.environ, CEFFIO_ROOM=jeton, CEFFIO_WORK=TRAVAIL)
	log = open(os.path.join(TRAVAIL, f"capture-{jeton}.log"), "w")
	proc = subprocess.Popen(["python3", "/app/capture.py"], env=environnement, stdout=log, stderr=subprocess.STDOUT)
	_captures[jeton] = {"proc": proc, "debut": time.time(), "webm_avant": None,
		"langue": langue, "recording": False}
	print(f"capture started for {jeton} (language {langue or 'auto'})", flush=True)


def nettoyer():
	"""Drop the working files once the call is over."""
	for reste in glob.glob(os.path.join(PISTES, "*.opus")) + glob.glob(os.path.join(PISTES, "*.json")):
		try:
			os.remove(reste)
		except FileNotFoundError:
			pass
	for reste in ("transcription.srt", "webm.webm"):
		try:
			os.remove(os.path.join(TRAVAIL, reste))
		except FileNotFoundError:
			pass


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


def attendre_webm(jeton, avant, delai=180):
	"""Wait for the .webm that did not exist when the capture started.

	Talk uploads it only once the recorder has finished: checking once, at the
	stop, found nothing and the transcript was dropped (first live run).
	"""
	fin = time.time() + delai
	while time.time() < fin:
		webm = nouveaux_webm(jeton, avant)
		if webm:
			return webm
		time.sleep(3)
	return ""


def nouveaux_webm(jeton, avant):
	for chemin in webm_de_la_salle(jeton):
		if chemin not in avant and os.path.basename(chemin) != "":
			return chemin
	return ""


def noms(jeton, sessions):
	"""HPB session -> name, straight from the roster the capture wrote.

	WHY NOT THE DATABASE: the tracks are named by the SIGNALING session id,
	while oc_talk_sessions stores the Nextcloud one — querying it with the
	wrong identifier returned nothing and the transcript fell back to
	"participant abcdef". The roster carries both, and the user id with them.
	"""
	chemin = os.path.join(PISTES, "participants.json")
	if not os.path.exists(chemin):
		return {}
	roster = json.load(open(chemin))
	return {sid: desc.get("userId") or desc.get("displayName") or sid[:10]
		for sid, desc in roster.items()
		if sid in sessions and (desc.get("userId") or desc.get("displayName"))}


def deposer(jeton, etat, chemin_webm):
	"""Write ONLY the .srt next to the recording.

	WHY BASE64 IN THE COMMAND AND NOT STDIN: the Kubernetes client in this
	version has no close_stdin(), so `cat > file` never sees the end of its
	input and the call hangs (observed on the first live deposit). A subtitle
	file is a few kilobytes: base64 in the argument is bounded and simple.
	A binary recording would not fit — but nothing binary crosses this path any
	more.
	"""
	base = chemin_webm[:-len(".webm")]
	pod = pod_de(NS_FILES, "app=nextcloud")
	srt = os.path.join(TRAVAIL, "transcription.srt")
	with open(srt, "rb") as f:
		donnees = base64.b64encode(f.read()).decode("ascii")

	executer(NS_FILES, pod, ["sh", "-c", "printf %%s '%s' | base64 -d > '%s.srt'" % (donnees, base)])
	executer(NS_FILES, pod, ["sh", "-c", "chown www-data:www-data '%s.srt'" % base])
	dossier = os.path.dirname(chemin_webm)
	executer(NS_FILES, pod, ["su", "-s", "/bin/sh", "www-data", "-c",
		"php occ files:scan --path=\"%s\"" % dossier[len("/var/www/html/data/"):]])
	print("deposited %s.srt" % base, flush=True)


def finaliser(jeton, etat):
	webm = attendre_webm(jeton, etat["webm_avant"])
	if not webm:
		print(f"no new .webm for {jeton}: transcript not deposited", flush=True)
		return
	nom = os.path.basename(webm)[: -len(".webm")].replace("Recording ", "").replace(" ", "T")
	import calendar

	debut_webm = float(calendar.timegm(time.strptime(nom + "Z", "%Y-%m-%dT%H-%M-%SZ")))

	# THE MIX IS FETCHED, to MEASURE the alignment instead of assuming it.
	# It travels base64-encoded: the exec stream decodes stdout as UTF-8, so a
	# binary read comes back corrupted.
	pod = pod_de(NS_FILES, "app=nextcloud")
	try:
		encode = executer(NS_FILES, pod, ["sh", "-c", "base64 -w0 '%s'" % webm])
		with open(os.path.join(TRAVAIL, "webm.webm"), "wb") as f:
			f.write(base64.b64decode(encode))
	except Exception as e:
		print("mix not fetched (%r): alignment falls back to timestamps" % e, flush=True)

	sessions = [os.path.basename(p)[:-len(".opus")] for p in os.listdir(PISTES) if p.endswith(".opus")]
	with open(os.path.join(PISTES, "noms.json"), "w") as f:
		json.dump(noms(jeton, sessions), f)

	environnement = dict(os.environ, CEFFIO_LANGUE=etat.get("langue", ""),
		CEFFIO_MIX=os.path.join(TRAVAIL, "webm.webm"))
	subprocess.run(["python3", "/app/finaliser.py", PISTES, os.path.join(TRAVAIL, "transcription.srt"), str(debut_webm)],
		env=environnement, check=False)
	deposer(jeton, etat, webm)
def boucle():
	while True:
		try:
			in_call = set(rooms_in_call())
			recording = {jeton: langue for jeton, _, langue in salons_enregistres()}

			# A new call: join it now, so the subscription is ready before
			# anyone presses record.
			for jeton in in_call:
				if jeton not in _captures:
					demarrer(jeton, recording.get(jeton, ""))

			# A recording just started in a room we already joined: remember
			# the recordings that existed, so the new .webm can be identified.
			for jeton in recording:
				etat = _captures.get(jeton)
				if etat and not etat["recording"]:
					etat["recording"] = True
					etat["webm_avant"] = set(webm_de_la_salle(jeton))
					etat["langue"] = recording[jeton] or etat.get("langue", "")
					print("recording started in %s" % jeton, flush=True)

			# A recording stopped: finalise, but keep capturing while the call
			# goes on (a second recording would reuse the same tracks).
			for jeton, etat in list(_captures.items()):
				if etat["recording"] and jeton not in recording:
					etat["recording"] = False
					finaliser(jeton, etat)

			# The call ended: stop capturing.
			for jeton in list(_captures):
				if jeton not in in_call:
					arreter(jeton)
					nettoyer()
		except Exception as e:
			print(f"loop: {e!r}", flush=True)
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
