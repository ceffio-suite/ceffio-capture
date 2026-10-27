"""Authenticity and meaning of the relay's notifications.

WHY THIS MODULE EXISTS SEPARATELY: the part that must be right — "this
notification is authentic, and this is what it asks for" — has to be testable
without a cluster, a signaling connection or a Whisper model. agent.py owns the
effects; this module owns the judgment, and its tests run in a second on any
machine (tests/test_notification.py, standard library only).

THE SIGNATURE IS THE RELAY'S, AND THE RELAY'S IS THE RECORDER'S: hex string of
HMAC-SHA256(secret, random || body), compared in constant time. The scheme is
written again here rather than imported because it belongs to the protocol, not
to a shared library — and the test vectors come from the reference
implementation, so what is proven is interoperability.

A NOTIFICATION THAT FAILS THE CHECK IS REFUSED, NOT IGNORED: the endpoint
answers 403 and nothing is started or stopped. A forged "stop" that was merely
logged would still be a way to end a transcription.
"""
import hashlib
import hmac
import json
import re
import threading
import time
from datetime import datetime

# The relay sends a few hundred bytes. The limit is here so that a caller with
# the secret — or one without — cannot make this process read a report.
MAX_BODY = 4096

# LA FENÊTRE ANTI-REJEU, en secondes.
FENETRE = 120

TYPES = ("start", "stop")

# LE JETON N'EST COUVERT PAR AUCUNE SIGNATURE : Talk signe random + body, pas le
# chemin. Il arrive donc d'une requête authentique dont UN CHAMP n'est pas
# authentifié, et il finit dans des chemins de fichiers. On n'accepte que la
# forme qu'un jeton Talk peut avoir — alphanumérique — ce qui exclut '/' , '.',
# ';', '|', '$', l'espace et les apostrophes.
#
# L'ALPHABET EXACT DE TALK est plus étroit (8 caractères parmi CHAR_LOWER +
# CHAR_DIGITS privés de "l", "0", "1", ou 10 chiffres si SIP est configuré),
# mais le resserrer rejetterait les jetons d'une autre version ou d'une autre
# configuration pour un gain de sécurité nul : ce qu'on veut interdire, c'est le
# métacaractère, pas une lettre.
JETON = re.compile(r"[A-Za-z0-9]{4,64}")


class Refus(Exception):
	"""A notification that must not be acted on, with the status to answer."""

	def __init__(self, raison, code=400):
		super().__init__(raison)
		self.code = code


def signature(secret, aleatoire, corps):
	"""The recorder's checksum: hex HMAC-SHA256 over random || body."""
	return hmac.new(secret.encode(), aleatoire.encode() + corps, hashlib.sha256).hexdigest()


def verifie(secret, aleatoire, somme, corps):
	"""Constant-time comparison, so a wrong signature leaks nothing."""
	if not secret or not aleatoire or not somme:
		return False
	return hmac.compare_digest(somme, signature(secret, aleatoire, corps))


def lire(corps):
	"""Parse one notification, or refuse it.

	THE FIELDS ARE REQUIRED, and an absent token is refused rather than
	defaulted: a notification that does not name a room cannot be carried out,
	and guessing "the last one" is how a transcript ends up in the wrong
	meeting.
	"""
	if len(corps) > MAX_BODY:
		raise Refus("body too large", 413)
	try:
		avis = json.loads(corps)
	except ValueError:
		raise Refus("body is not JSON")
	if not isinstance(avis, dict):
		raise Refus("body is not an object")
	if avis.get("type") not in TYPES:
		raise Refus("unknown notification type: %r" % (avis.get("type"),))
	jeton = avis.get("token")
	if not isinstance(jeton, str) or not JETON.fullmatch(jeton):
		# UN SEUL REFUS POUR "ABSENT" ET "MAL FORMÉ" : le message n'a pas à
		# expliquer à un appelant ce qui manque à son jeton.
		raise Refus("invalid room token")
	return avis


class Etat:
	"""Which rooms are being recorded, and the idempotence rules.

	THE RULES ARE THE PLAN'S, and both were written from a real failure mode:
	a duplicate "start" (the relay retries nothing, but Nextcloud can) must not
	open a second capture that would fight the first for the same tracks, and a
	"stop" without a capture is logged and ignored rather than finalising
	whatever happens to be in the work directory.
	"""

	def __init__(self):
		self._salles = {}  # token -> True while a capture is running
		self._verrou = threading.Lock()

	def commence(self, token):
		"""(True, False) for a start to carry out, (False, True) for a duplicate."""
		with self._verrou:
			if self._salles.get(token):
				return False, True
			self._salles[token] = True
			return True, False

	def termine(self, token):
		"""True when this stop closes a capture, False when there is none."""
		with self._verrou:
			if not self._salles.get(token):
				return False
			del self._salles[token]
			return True


class Rejeu:
	"""Refuse une notification rejouée, ou trop vieille.

	POURQUOI LA SIGNATURE NE SUFFIT PAS : elle prouve que le message vient du
	relais, pas QU'IL EST NOUVEAU. Une notification capturée — dans un journal, un
	proxy, une capture de trafic — peut être renvoyée telle quelle, et la capture
	ouvrirait une seconde transcription pour le même salon, ou arrêterait celle
	qui vient de commencer.

	DEUX BARRIÈRES : le champ `at` borne la fenêtre (deux minutes, largement plus
	que le délai réel d'un relais local), et le nonce `Ceffio-Relay-Random` est
	mémorisé pendant cette même fenêtre — deux corps identiques ne peuvent donc
	pas être acceptés deux fois.
	"""

	def __init__(self, fenetre=FENETRE, horloge=None):
		self._fenetre = fenetre
		self._horloge = horloge or time.time
		self._vus = {}
		self._verrou = threading.Lock()

	def accepter(self, aleatoire, at):
		"""Rend None si la notification est acceptable, la raison sinon."""
		maintenant = self._horloge()
		if not at:
			return "no timestamp"
		try:
			quand = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
		except ValueError:
			return "invalid timestamp"
		if abs(maintenant - quand) > self._fenetre:
			return "timestamp outside the window"
		with self._verrou:
			# PURGE À CHAQUE APPEL : la mémoire ne suit pas la durée de vie du
			# processus, elle suit la fenêtre.
			for nonce, expiration in list(self._vus.items()):
				if expiration <= maintenant:
					del self._vus[nonce]
			if aleatoire in self._vus:
				return "replayed notification"
			self._vus[aleatoire] = maintenant + self._fenetre
		return None
