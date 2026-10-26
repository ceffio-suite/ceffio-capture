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
import threading

# The relay sends a few hundred bytes. The limit is here so that a caller with
# the secret — or one without — cannot make this process read a report.
MAX_BODY = 4096

TYPES = ("start", "stop")


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
	if not avis.get("token"):
		raise Refus("no room token")
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
