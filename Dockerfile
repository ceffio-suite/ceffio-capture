# Ce service capture l'audio PAR PARTICIPANT pendant un enregistrement Talk,
# puis transcrit chaque piste sur le CPU. Il est séparé du binaire Ceffio parce
# que la pile est Python (WebRTC, whisper) alors que le binaire est statique.
#
# Ce qui n'est PAS dans l'image : les modèles. Ils vivent dans le volume monté
# sur /models et sont téléchargés au premier usage — une image de 1,5 Go à
# chaque version serait payée par toutes les installations, y compris celles
# qui n'utilisent jamais la transcription.
FROM python:3.12-slim

# ffmpeg : incrustation des sous-titres dans le .webm (copie de flux, pas de
# réencodage — quelques centaines d'octets).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

ENV CEFFIO_WORK=/work \
    CEFFIO_MODELS=/models

EXPOSE 8080

# Le client de signaling est celui de l'app de transcription (AGPL, vendé dans
# app/lib avec sa provenance) : ce chemin est éprouvé, le réécrire serait un
# risque, pas une simplification.
CMD ["python3", "/app/agent.py"]
