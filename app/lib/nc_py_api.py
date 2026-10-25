"""Substitut minimal de nc_py_api pour le service de capture.

Le client de signaling est repris de l'app de transcription, où Nextcloud lui
fournit l'URL du backend et ses réglages HPB. Ici, rien de tout cela n'existe :
la configuration vient de l'environnement (CEFFIO_NEXTCLOUD_URL, CEFFIO_HPB_URL,
CEFFIO_INTERNAL_SECRET), et les seules fonctions réellement utilisées par le
client sont l'URL du backend et le secret interne.

Toute autre fonction lève : un substitut muet laisserait croire que la
traduction ou l'AppAPI sont disponibles.
"""
import os


class NextcloudException(Exception):
	pass


class _AppCfg:
	def __init__(self):
		self.endpoint = os.environ.get("CEFFIO_NEXTCLOUD_URL", "")


class NextcloudApp:
	def __init__(self, *args, **kwargs):
		self.app_cfg = _AppCfg()

	def ocs(self, *args, **kwargs):
		raise RuntimeError(
			"nc_py_api n'est pas disponible dans le service de capture : "
			"la configuration vient de l'environnement"
		)


class AsyncNextcloudApp(NextcloudApp):
	pass
