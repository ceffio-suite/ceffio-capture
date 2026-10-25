"""Substitut du transcripteur Vosk de l'app de transcription.

La capture remplace ce transcripteur par une écriture sur disque (voir
`capture.py`, qui réassigne `spreed_client.VoskTranscriber`). Cette classe
n'existe que pour satisfaire l'import du client ; l'instancier sans la
remplacer est une erreur de programmation, dite explicitement.
"""


class VoskTranscriber:
	def __init__(self, *args, **kwargs):
		raise NotImplementedError(
			"la capture n'utilise pas VoskTranscriber : elle écrit les pistes "
			"sur disque (voir PisteWav dans capture.py)"
		)
