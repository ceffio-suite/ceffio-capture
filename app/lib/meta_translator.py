"""Substituts des deux collaborateurs de l'app de transcription que la capture
n'utilise pas.

Le client de signaling instancie un `MetaTranslator` (traduction en direct) et
un `VoskTranscriber` (transcription en direct). La capture fait autre chose :
elle écrit les pistes sur disque et transcrit APRÈS l'appel, avec un autre
moteur. Ces deux classes existent donc uniquement pour que le client se
construise ; l'une répond « non » à toute question de traduction, l'autre
échoue franchement si on lui demande de transcrire.
"""


class MetaTranslator:
	def __init__(self, *args, **kwargs):
		pass

	async def is_translating(self) -> bool:
		return False

	async def translate(self, *args, **kwargs):
		return None
