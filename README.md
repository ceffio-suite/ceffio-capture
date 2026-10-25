# ceffio-capture

Capture l'audio **par participant** pendant un enregistrement Talk, puis
transcrit chaque piste sur le CPU.

## Ce que ça fait

Quand un enregistrement démarre dans un appel Talk, ce service rejoint l'appel
comme client interne (par le signaling, avec le secret partagé) et reçoit du
SFU **une piste audio par participant** — là où le `.webm` de Talk n'a qu'un
mélange. À l'arrêt :

- une piste **Opus 32 kbit/s** par participant (~14 Mo/h), à côté du `.webm` ;
- un **`.srt` nommé** (qui parle), recalé sur le début du `.webm` ;
- les sous-titres **incrustés dans le `.webm`** (piste WebVTT, copie de flux).

La transcription est faite par **faster-whisper en int8, CPU uniquement**
(mesuré ×7–11 la durée de l'audio sur 8 threads), avec détection de la langue
**par piste** : deux participants peuvent parler deux langues différentes.

## Déploiement

L'image est déployée par le module `capture` du dépôt
[ceffio](https://github.com/ceffio-suite/ceffio) :

```sh
ceffio module install capture
```

Variables attendues :

| Variable | Rôle |
|---|---|
| `CEFFIO_HPB_URL` | `wss://spreed.<domaine>/spreed` |
| `CEFFIO_NEXTCLOUD_URL` | `https://cloud.<domaine>` |
| `CEFFIO_INTERNAL_SECRET` | secret partagé du signaling |
| `CEFFIO_WORK` | répertoire de travail (défaut `/work`) |
| `CEFFIO_MODELS` | volume des modèles (défaut `/models`) |
| `CEFFIO_MODEL` | `small` (défaut), `medium`, … |
| `CEFFIO_THREADS` | threads CPU pour la transcription (défaut 4) |

Droits Kubernetes nécessaires (créés par le module) : `pods/exec` dans le
namespace Nextcloud (dépôt des fichiers, `occ files:scan`) et dans celui de la
base (état d'enregistrement, langue de la salle).

## Construction

```sh
docker build --platform linux/amd64 -t ceffio-capture .
```

Les modèles **ne sont pas** dans l'image : ils sont téléchargés au premier
usage dans le volume monté sur `/models`.

## Provenance

Le client de signaling (`app/lib/spreed_client.py` et ses modules) vient de
l'application [live_transcription](https://github.com/nextcloud/live_transcription)
de Nextcloud (AGPL-3.0). Il est vendé et adapté : la configuration vient de
l'environnement au lieu d'AppAPI, et le transcripteur en direct est remplacé
par une écriture sur disque. C'est ce chemin qui est éprouvé en production —
le réécrire serait un risque, pas une simplification.

## Licence

AGPL-3.0-or-later. Voir [LICENSE](LICENSE).
