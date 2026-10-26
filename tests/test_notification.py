"""Tests of the relay's notifications. Standard library only: they run in a
second, on any machine, without a cluster, a model or a network.

THE VECTORS COME FROM THE REFERENCE IMPLEMENTATION — Server.py,
_calculateChecksum: hmac.new(secret, random.encode() + body, sha256).hexdigest()
— so a change that still agrees with itself but no longer agrees with the
recorder fails here.
"""
import json
import os
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
