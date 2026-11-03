# Pinned to an exact Python release and image digest: the image only changes when we choose to.
FROM python:3.13.18-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

LABEL org.opencontainers.image.title="Dubarr" \
      org.opencontainers.image.description="Language rules for Sonarr and Radarr: the right audio language per kind of title" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/osakesans/dubarr" \
      com.centurylinklabs.watchtower.enable="false"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DUBARR_CONFIG_DIR=/config DUBARR_PORT=8791
# mkvtoolnix edits the default audio flag in MKV headers (nothing is re-encoded)
RUN apt-get update && apt-get install -y --no-install-recommends mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY dubarr /app/dubarr
RUN useradd --uid 1000 --create-home dubarr && mkdir -p /config && chown dubarr /config
USER 1000
VOLUME /config
EXPOSE 8791
HEALTHCHECK --interval=5m --timeout=15s --start-period=3m --start-interval=15s --retries=3 \
  CMD ["python", "-m", "dubarr.healthcheck"]
CMD ["python", "-m", "dubarr"]
