"""Agent du service de capture.

START AND STOP ARRIVE FROM THE RELAY, and from nothing else. The recorder's pod
carries a relay in front of it; when the recorder has accepted a start or a stop,
the relay posts it here, signed. This service no longer reads the database — no
one-second poll on oc_talk_rooms, no psql, no credential on the data namespace —
and the only thing it still asks the cluster for is the deposit of the finished
transcript, through the Nextcloud pod.

The language of the room is NOT in the notification: see demarrer() for what
that costs and why it is accepted.

Deposit goes through the Kubernetes API (exec in the Nextcloud pod): the service
needs neither database credentials nor access to the Nextcloud volume.
"""
import base64
import glob
import queue
import shutil
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from kubernetes import client, config, stream

import notification

NS_FILES = os.environ.get("CEFFIO_NC_NAMESPACE", "ceffio-files")
NS_VIDEO = os.environ.get("CEFFIO_NAMESPACE", "ceffio-video")
TRAVAIL = os.environ.get("CEFFIO_WORK", "/work")

# THE RELAY'S SECRET, COPIED INTO THIS MODULE'S OWN VAULT ENTRY: the pod holds
# no right on the recording server's entry, exactly as it holds none on the
# signaling's. Empty means the endpoint refuses everything — a capture that
# accepts an unsigned start would let any pod in the namespace open a
# transcription.
SECRET_RELAIS = os.environ.get("CEFFIO_RELAY_SECRET", "")

# A CAPTURE THAT NEVER RECEIVES ITS STOP MUST NOT FILL THE DISK. The relay
# always sends one, but a pod restarted mid-recording, or a relay that died,
# would otherwise leave a client writing audio until the volume is full.
DUREE_MAX = float(os.environ.get("CEFFIO_MAX_HOURS", "6")) * 3600

# How long the client is given to close its files when the marker disappears,
# before it is signalled. A variable so a test can shorten it.
DELAI_ARRET = float(os.environ.get("CEFFIO_STOP_GRACE", "10"))

_FILE = queue.Queue()
_etat = notification.Etat()


def dossier(jeton):
	"""One work directory PER ROOM.

	A single shared directory mixed two simultaneous meetings: the words of B
	ended up in the transcript deposited for A's owner, and finishing A deleted
	B's tracks. Nothing outside the room's own folder is touched now.
	"""
	return os.path.join(TRAVAIL, "salons", jeton)


def pistes(jeton):
	return os.path.join(dossier(jeton), "pistes")


def marqueur(jeton):
	"""Present only while that room is being recorded."""
	return os.path.join(dossier(jeton), "recording")
# THE LANGUAGE IS NO LONGER THE ROOM'S: see demarrer(). CEFFIO_LANGUE, when
# set, is the installation's default; empty means whisper detects it per track.
LANGUE_DEFAUT = os.environ.get("CEFFIO_LANGUE", "")

# WHAT THE LAST NOTIFICATION ASKED FOR, on /status: without it, an operator
# seeing "no capture" cannot tell a quiet installation from a refused start.
_derniere_notification = {"type": None, "token": None, "at": None, "outcome": None}

_api = None

# THE LAST JOB, exposed on GET /status. A silent failure was listed as a HIGH
# risk in the plan: the module cannot tell "working" from "failing" unless the
# service says what happened last. Nothing of the transcript itself is exposed —
# only what an operator needs to see that it ran.
_dernier = {"room": None, "owner": None, "started": None, "ended": None,
	"result": "none", "error": None, "seconds": None}

# THE MODEL IS LOADED AT STARTUP, before any meeting: an operator must learn
# that the chosen model is unusable when they look at the module state, not
# when a meeting has already been recorded.
_modele = {"model_ready": None, "model_effective": None, "model_error": None}
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


def webm_de_la_salle(jeton):
	pod = pod_de(NS_FILES, "app=nextcloud")
	# splitlines(), NOT split(): a recording path contains spaces
	# ("Recording 2026-10-25 23-00-52.webm"), and splitting on whitespace
	# returned fragments that parse as a time and fail.
	return [ligne.strip() for ligne in executer(NS_FILES, pod, ["sh", "-c",
		f"ls -1 /var/www/html/data/*/files/Talk/Recording/{jeton}/*.webm 2>/dev/null"]).splitlines() if ligne.strip()]


def demarrer(jeton):
	"""Open one capture for a room the relay has just seen start recording.

	THE LANGUAGE OF THE ROOM IS NO LONGER KNOWABLE HERE, and that is the price
	of dropping the database. It used to be read from oc_talk_rooms; the relay's
	notification does not carry it, and the signaling does not put it in the
	join reply that the vendored client reads. So this keeps what the plan
	allows: whisper detects the language per track, and CEFFIO_LANGUE, when set,
	overrides it for the whole installation. The consequence is documented in
	docs/plan-capture.md: a French call that ends with one Arabic sentence can
	come back wholly in Arabic.
	"""
	os.makedirs(pistes(jeton), exist_ok=True)
	# THE MARKER COMES BEFORE THE CLIENT: the client subscribes and discards
	# until it exists, so a capture that fails to start leaves no audio behind.
	open(marqueur(jeton), "w").close()
	try:
		avant = set(webm_de_la_salle(jeton))
	except Exception as e:
		# WITHOUT THE SNAPSHOT the finaliser could mistake the room's PREVIOUS
		# .webm for this recording's. Starting anyway is the lesser evil: the
		# audio is irreplaceable, and the error is said out loud.
		avant = set()
		print(f"webm snapshot failed for {jeton}: {e!r}", flush=True)
	environnement = dict(os.environ, CEFFIO_ROOM=jeton, CEFFIO_WORK=dossier(jeton))
	log = open(os.path.join(dossier(jeton), "capture.log"), "w")
	proc = subprocess.Popen(["python3", "/app/capture.py"], env=environnement, stdout=log, stderr=subprocess.STDOUT)
	_captures[jeton] = {"proc": proc, "debut": time.time(), "webm_avant": avant,
		"langue": LANGUE_DEFAUT, "recording": True}
	print(f"capture started for {jeton} (language {LANGUE_DEFAUT or 'auto'})", flush=True)


def nettoyer(jeton):
	"""Drop ONE room's working files — and never a failed transcription.

	A failed transcription that also deletes its tracks is a transcription lost
	for good: the audio existed once. The folder of a failed job is kept
	(CEFFIO_KEEP_FAILED_HOURS, 24 h by default) so it can be retried.
	"""
	base = dossier(jeton)
	dernier = _dernier.get("room") == jeton and _dernier.get("result") == "failed"
	if dernier:
		age = time.time() - os.path.getmtime(base) if os.path.exists(base) else 0
		if age < float(os.environ.get("CEFFIO_KEEP_FAILED_HOURS", "24")) * 3600:
			print("keeping %s: its transcription failed" % base, flush=True)
			return
	shutil.rmtree(base, ignore_errors=True)


def arreter(jeton):
	etat = _captures.pop(jeton, None)
	if not etat:
		return
	# THE MARKER GOES FIRST: it is what tells the client to keep writing, so
	# removing it before the process is stopped avoids a last track opened
	# between the two.
	try:
		os.remove(marqueur(jeton))
	except FileNotFoundError:
		pass
	# THE CLIENT WRITES ITS FILES WHEN IT CLOSES, not while it runs: PyAV keeps
	# the audio in memory and flushes at close(). Measured in this container:
	# fifty packets leave the file at ZERO bytes, and it holds 387 after close().
	# So the marker above is the signal, and the wait below is the time it takes
	# to notice it — a SIGTERM first would destroy the meeting's audio.
	try:
		etat["proc"].wait(timeout=DELAI_ARRET)
	except subprocess.TimeoutExpired:
		# ONLY IF IT DOES NOT COME BACK: a client stuck on a dead socket.
		etat["proc"].terminate()
		try:
			etat["proc"].wait(timeout=5)
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
	chemin = os.path.join(pistes(jeton), "participants.json")
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
	srt = os.path.join(dossier(jeton), "transcription.srt")
	with open(srt, "rb") as f:
		donnees = base64.b64encode(f.read()).decode("ascii")

	executer(NS_FILES, pod, ["sh", "-c", "printf %%s '%s' | base64 -d > '%s.srt'" % (donnees, base)])
	executer(NS_FILES, pod, ["sh", "-c", "chown www-data:www-data '%s.srt'" % base])
	# THE LOCAL NAME MUST NOT SHADOW dossier(), THE FUNCTION: the first version
	# assigned `dossier = os.path.dirname(...)` here, which made the earlier
	# `dossier(jeton)` a local variable read before assignment — a
	# UnboundLocalError, on the ONE line that writes the transcript. Observed on
	# the first real recording: the transcription succeeded, and the file was
	# never deposited.
	executer(NS_FILES, pod, ["su", "-s", "/bin/sh", "www-data", "-c",
		"php occ files:scan --path=\"%s\"" % chemin_scan(chemin_webm)])
	print("deposited %s.srt" % base, flush=True)


# RACINE_NC is where Nextcloud keeps the users' files inside its pod.
RACINE_NC = "/var/www/html/data/"


def chemin_scan(chemin_webm):
	"""The path `occ files:scan --path` expects: relative to the data directory.

	WHY RELATIVE: occ resolves --path under the data directory. The absolute
	path makes it look for a user named "var", answer "user var not found", and
	the transcript stays on disk, invisible in the interface — a deposit that
	looks done and is not.
	"""
	dossier_webm = os.path.dirname(chemin_webm)
	if dossier_webm.startswith(RACINE_NC):
		return dossier_webm[len(RACINE_NC):]
	return dossier_webm


def finaliser(jeton, etat):
	webm = attendre_webm(jeton, etat["webm_avant"])
	if not webm:
		# A MISSING .webm IS A FAILED JOB, not a quiet evening: the transcript
		# has nowhere to go, and /status must say it. Left at "none", an operator
		# sees a healthy module and a meeting that will never be transcribed.
		# The work directory is kept (see nettoyer) so the audio can be retried.
		_dernier.update({"room": jeton, "result": "failed", "ended": time.time(),
			"error": "no .webm to attach the transcript to"})
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
		with open(os.path.join(dossier(jeton), "webm.webm"), "wb") as f:
			f.write(base64.b64decode(encode))
	except Exception as e:
		print("mix not fetched (%r): alignment falls back to timestamps" % e, flush=True)

	sessions = [os.path.basename(p)[:-len(".opus")] for p in os.listdir(pistes(jeton)) if p.endswith(".opus")]
	with open(os.path.join(pistes(jeton), "noms.json"), "w") as f:
		json.dump(noms(jeton, sessions), f)

	environnement = dict(os.environ, CEFFIO_LANGUE=etat.get("langue", ""),
		CEFFIO_MIX=os.path.join(dossier(jeton), "webm.webm"))
	debut = time.time()
	_dernier.update({"room": jeton, "started": debut, "result": "running", "error": None})
	try:
		copie = subprocess.run(["python3", "/app/finaliser.py", pistes(jeton),
			os.path.join(dossier(jeton), "transcription.srt"), str(debut_webm)],
			env=environnement, check=False)
		if copie.returncode != 0:
			raise RuntimeError("transcription exited with %d" % copie.returncode)
		deposer(jeton, etat, webm)
		_dernier.update({"result": "transcribed", "ended": time.time(),
			"seconds": round(time.time() - debut, 1)})
	except Exception as e:
		_dernier.update({"result": "failed", "error": repr(e), "ended": time.time()})
		print("finalisation failed: %r" % e, flush=True)
def servir_un(avis):
	"""Carry out ONE notification. Called by the worker, never by the request.

	THE JUDGMENT IS notification.Etat's, the effects are here: the endpoint must
	answer in milliseconds, and a transcription takes minutes.
	"""
	jeton = avis["token"]
	_derniere_notification.update({"type": avis["type"], "token": jeton,
		"at": avis.get("at"), "outcome": None})

	if avis["type"] == "start":
		commence, doublon = _etat.commence(jeton)
		if doublon:
			print(f"start ignored for {jeton}: already recording", flush=True)
			_derniere_notification["outcome"] = "start ignored: already recording"
			return
		demarrer(jeton)
		_derniere_notification["outcome"] = "capture started"
		return

	if not _etat.termine(jeton):
		# THE PLAN'S RULE, and it protects the previous recording's files: a
		# stop with no capture would finalise whatever the work directory still
		# holds.
		print(f"stop ignored for {jeton}: no recording in progress", flush=True)
		_derniere_notification["outcome"] = "stop ignored: no recording in progress"
		return
	etat = arreter(jeton)
	_derniere_notification["outcome"] = "capture stopped, transcription under way"
	if etat:
		finaliser(jeton, etat)
	nettoyer(jeton)


def surveiller():
	"""Stop a capture that never received its stop.

	The relay always sends one; a capture left running by a crash, a lost
	notification or a relay killed mid-recording would write audio until the
	volume fills. Six hours is longer than any meeting this appliance is for.
	"""
	for jeton, etat in list(_captures.items()):
		if time.time() - etat["debut"] <= DUREE_MAX:
			continue
		print(f"capture for {jeton} exceeded {DUREE_MAX / 3600:.0f} h: stopping it", flush=True)
		if _etat.termine(jeton):
			arret = arreter(jeton)
			if arret:
				finaliser(jeton, arret)
			nettoyer(jeton)


def servir():
	"""The single worker: notifications are carried out IN ORDER.

	WHY ONE WORKER AND NOT A THREAD PER NOTIFICATION: two captures in the same
	room would fight over the same work directory, and the deposit assumes one
	finalisation at a time. The polling loop had the same property; the queue
	replaces the poll, and the order is now the relay's.
	"""
	while True:
		try:
			avis = _FILE.get(timeout=60)
		except queue.Empty:
			avis = None
		if avis is not None:
			try:
				servir_un(avis)
			except Exception as e:
				print(f"notification {avis.get('type')!r} failed: {e!r}", flush=True)
			finally:
				_FILE.task_done()
		try:
			surveiller()
		except Exception as e:
			print(f"watchdog: {e!r}", flush=True)


class Sante(BaseHTTPRequestHandler):
	def _repond(self, code, corps):
		self.send_response(code)
		self.send_header("Content-Type", "text/plain; charset=utf-8")
		self.send_header("Content-Length", str(len(corps)))
		self.end_headers()
		self.wfile.write(corps)

	def do_GET(self):
		if self.path == "/status":
			corps = json.dumps({**_dernier, **_modele,
				"last_notification": _derniere_notification}).encode()
			self.send_response(200)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(corps)))
			self.end_headers()
			self.wfile.write(corps)
			return
		self.send_response(200 if self.path == "/healthz" else 404)
		self.end_headers()
		self.wfile.write(b"ok")

	def do_POST(self):
		"""POST /v1/recording — the relay's notification, and the only entry.

		THE ORDER IS SIGNATURE FIRST, PARSING SECOND: acting on a body before it
		is proven to come from the relay would let any pod in the namespace open
		a transcription, or end one.
		"""
		if self.path != "/v1/recording":
			self._repond(404, b"not found")
			return
		if not SECRET_RELAIS:
			# FAIL CLOSED, AND SAY SO: an installation whose secret was not
			# copied would otherwise accept a signature anyone can compute with
			# an empty key.
			self._repond(503, b"no relay secret configured")
			return
		try:
			longueur = int(self.headers.get("Content-Length") or 0)
		except ValueError:
			longueur = 0
		if longueur <= 0:
			self._repond(400, b"no body")
			return
		if longueur > notification.MAX_BODY:
			self._repond(413, b"body too large")
			return

		corps = self.rfile.read(longueur)
		if not notification.verifie(SECRET_RELAIS,
				self.headers.get("Ceffio-Relay-Random", ""),
				self.headers.get("Ceffio-Relay-Checksum", ""), corps):
			print("notification refused: bad signature", flush=True)
			self._repond(403, b"bad signature")
			return
		try:
			avis = notification.lire(corps)
		except notification.Refus as refus:
			self._repond(refus.code, str(refus).encode())
			return

		# 202, NOT 200: the work is queued, not done. The relay gives us two
		# seconds and does not retry, so nothing here may wait for a capture to
		# open or a transcription to finish.
		_FILE.put(avis)
		self._repond(202, b"accepted")

	def log_message(self, *args):
		pass


def verifier_modele():
	"""Load the configured model once, and say whether it worked."""
	modele = os.environ.get("CEFFIO_MODEL", "small")
	try:
		from faster_whisper import WhisperModel
		WhisperModel(modele, device="cpu", compute_type="int8",
			cpu_threads=1, download_root=os.environ.get("CEFFIO_MODELS", "/models"))
		_modele.update({"model_ready": True, "model_effective": modele})
	except Exception as e:
		_modele.update({"model_ready": False, "model_effective": "small", "model_error": repr(e)})


if __name__ == "__main__":
	os.makedirs(os.path.join(TRAVAIL, "salons"), exist_ok=True)
	if not SECRET_RELAIS:
		print("ATTENTION: CEFFIO_RELAY_SECRET is empty — every notification will be refused", flush=True)
	threading.Thread(target=verifier_modele, daemon=True).start()
	threading.Thread(target=servir, daemon=True).start()
	print("capture agent started: start and stop come from the relay, no database", flush=True)
	HTTPServer(("", 8080), Sante).serve_forever()
