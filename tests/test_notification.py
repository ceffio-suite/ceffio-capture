"""Tests of the relay's notifications. Standard library only: they run in a
second, on any machine, without a cluster, a model or a network.

THE VECTORS COME FROM THE REFERENCE IMPLEMENTATION — Server.py,
_calculateChecksum: hmac.new(secret, random.encode() + body, sha256).hexdigest()
— so a change that still agrees with itself but no longer agrees with the
recorder fails here.
"""
import json
import os
from datetime import datetime, timezone
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import notification  # noqa: E402

SECRET = "s3cret"
RANDOM = "abc123"
START = b'{"type":"start"}'
START_SIG = "cd85a0f4b6e64a200b1bf44f47d69f808bd90cae3d192f9004754a08a2285740"
STOP = b'{"type":"stop"}'
STOP_SIG = "53672f31b208762d0fcf3a1e54aa9e04ba3e09ed286c985678f653d484b963b4"


def avis(type_, **champs):
	corps = {"type": type_, "token": "salon1", "owner": "alice",
		"actor": {"type": "users", "id": "alice"},
		"backend": "https://cloud.exemple.org/", "at": "2026-10-26T20:00:00Z"}
	corps.update(champs)
	return json.dumps(corps).encode()


class TestSignature(unittest.TestCase):
	def test_vecteurs_de_reference(self):
		self.assertEqual(notification.signature(SECRET, RANDOM, START), START_SIG)
		self.assertEqual(notification.signature(SECRET, RANDOM, STOP), STOP_SIG)

	def test_accepte_la_bonne_signature(self):
		self.assertTrue(notification.verifie(SECRET, RANDOM, START_SIG, START))

	def test_refuse_une_signature_fausse(self):
		self.assertFalse(notification.verifie(SECRET, RANDOM, "0" * 64, START))

	def test_refuse_un_corps_modifie(self):
		self.assertFalse(notification.verifie(SECRET, RANDOM, START_SIG,
			START.replace(b"start", b"stop")))

	def test_refuse_un_autre_secret(self):
		self.assertFalse(notification.verifie("autre", RANDOM, START_SIG, START))

	def test_refuse_les_entetes_vides(self):
		# AN EMPTY SECRET MUST REFUSE, NOT ACCEPT BY ACCIDENT: hmac with an empty
		# key is a valid computation, and a service configured without its secret
		# would otherwise accept a signature anyone can compute.
		self.assertFalse(notification.verifie("", RANDOM, START_SIG, START))
		self.assertFalse(notification.verifie(SECRET, "", START_SIG, START))
		self.assertFalse(notification.verifie(SECRET, RANDOM, "", START))


class TestLecture(unittest.TestCase):
	def test_start_complet(self):
		avis_ = notification.lire(avis("start"))
		self.assertEqual(avis_["type"], "start")
		self.assertEqual(avis_["token"], "salon1")

	def test_stop_complet(self):
		self.assertEqual(notification.lire(avis("stop"))["type"], "stop")

	def test_type_inconnu_refuse(self):
		with self.assertRaises(notification.Refus) as ctx:
			notification.lire(avis("pause"))
		self.assertEqual(ctx.exception.code, 400)

	def test_sans_jeton_refuse(self):
		corps = json.loads(avis("start"))
		del corps["token"]
		with self.assertRaises(notification.Refus):
			notification.lire(json.dumps(corps).encode())

	def test_jetons_dangereux_refuses(self):
		# LA SIGNATURE NE COUVRE PAS LE CHEMIN : un jeton "authentique" peut
		# contenir n'importe quoi, et il finit dans des chemins de fichiers.
		for jeton in ["x;id", "../../etc", "", "a b", "tok/../x", "tok$(id)",
			"1234567890123456789012345678901234567890123456789012345678901234567890"]:
			corps = json.loads(avis("start"))
			corps["token"] = jeton
			with self.assertRaises(notification.Refus, msg="jeton %r accepté" % jeton) as ctx:
				notification.lire(json.dumps(corps).encode())
			self.assertEqual(ctx.exception.code, 400, "jeton %r : code %s" % (jeton, ctx.exception.code))

	def test_jetons_legitimes_acceptes(self):
		# 8 caractères alphanumériques (Talk), 10 chiffres (SIP), et la marge
		# de longueur qu'on s'autorise.
		for jeton in ["7kxkkuxm", "1234567890", "abcd", "A1b2C3d4"]:
			corps = json.loads(avis("start"))
			corps["token"] = jeton
			self.assertEqual(notification.lire(json.dumps(corps).encode())["token"], jeton)

	def test_non_json_refuse(self):
		with self.assertRaises(notification.Refus):
			notification.lire(b"pas du json")

	def test_liste_refusee(self):
		with self.assertRaises(notification.Refus):
			notification.lire(b'["start"]')

	def test_corps_trop_gros_refuse(self):
		with self.assertRaises(notification.Refus) as ctx:
			notification.lire(b"a" * (notification.MAX_BODY + 1))
		self.assertEqual(ctx.exception.code, 413)


class TestRejeu(unittest.TestCase):
	"""La signature prouve l'origine ; elle ne prouve pas la NOUVEAUTÉ."""

	def maintenant(self):
		return 1793200000.0  # une seconde fixe, pour un test déterministe

	def at(self, decalage):
		return datetime.fromtimestamp(self.maintenant() + decalage, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

	def test_notification_recente_acceptee(self):
		r = notification.Rejeu(horloge=self.maintenant)
		self.assertIsNone(r.accepter("nonce1", self.at(-5)))

	def test_notification_trop_vieille_refusee(self):
		r = notification.Rejeu(horloge=self.maintenant)
		self.assertIn("window", r.accepter("nonce1", self.at(-300)))

	def test_notification_trop_en_avance_refusee(self):
		# Une horloge en avance est aussi un signe de rejeu ou de falsification.
		r = notification.Rejeu(horloge=self.maintenant)
		self.assertIn("window", r.accepter("nonce1", self.at(+300)))

	def test_meme_nonce_deux_fois_refuse(self):
		r = notification.Rejeu(horloge=self.maintenant)
		self.assertIsNone(r.accepter("nonce1", self.at(-1)))
		self.assertIn("replay", r.accepter("nonce1", self.at(-1)))

	def test_nonce_reutilisable_apres_la_fenetre(self):
		instant = [self.maintenant()]
		r = notification.Rejeu(horloge=lambda: instant[0])
		maintenant = lambda: datetime.fromtimestamp(instant[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
		self.assertIsNone(r.accepter("nonce1", maintenant()))
		instant[0] += notification.FENETRE + 1
		self.assertIsNone(r.accepter("nonce1", maintenant()))

	def test_sans_horodatage_refuse(self):
		r = notification.Rejeu(horloge=self.maintenant)
		self.assertIn("timestamp", r.accepter("nonce1", None))

	def test_horodatage_illisible_refuse(self):
		r = notification.Rejeu(horloge=self.maintenant)
		self.assertIn("timestamp", r.accepter("nonce1", "hier"))


class TestEtat(unittest.TestCase):
	def test_start_puis_stop(self):
		etat = notification.Etat()
		self.assertEqual(etat.commence("salon1"), (True, False))
		self.assertTrue(etat.termine("salon1"))

	def test_start_en_double_est_ignore(self):
		etat = notification.Etat()
		etat.commence("salon1")
		# THE SECOND START IS A DUPLICATE, and it is reported as such: the caller
		# logs it rather than opening a capture that would fight the first.
		self.assertEqual(etat.commence("salon1"), (False, True))

	def test_stop_sans_capture_est_ignore(self):
		etat = notification.Etat()
		self.assertFalse(etat.termine("salon1"))

	def test_stop_en_double_est_ignore(self):
		etat = notification.Etat()
		etat.commence("salon1")
		etat.termine("salon1")
		self.assertFalse(etat.termine("salon1"))

	def test_une_nouvelle_capture_apres_un_stop(self):
		etat = notification.Etat()
		etat.commence("salon1")
		etat.termine("salon1")
		self.assertEqual(etat.commence("salon1"), (True, False))

	def test_les_salons_sont_independants(self):
		etat = notification.Etat()
		etat.commence("salon1")
		etat.commence("salon2")
		etat.termine("salon1")
		# salon2 continue : un stop dans un salon ne doit pas arrêter l'autre.
		self.assertEqual(etat.commence("salon2"), (False, True))


if __name__ == "__main__":
	unittest.main()
