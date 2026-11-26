FROM python:3.12.16-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
LABEL org.opencontainers.image.title="StreamRec" \
      org.opencontainers.image.description="Self-hosted live stream recorder with web UI" \
      org.opencontainers.image.source="https://github.com/MousaXD/streamerREC" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
# Verified against the immutable release asset digest in GitHub's release metadata.
ARG YTDLP_VERSION=2026.11.23
ARG YTDLP_SHA256=e8665ed64d6b9172773e812f891fb707a0cb1ee8d9ef4fc1acb00cabee4c5256
RUN curl --fail --location --proto '=https' --proto-redir '=https' \
    "https://github.com/yt-dlp/yt-dlp/releases/download/${YTDLP_VERSION}/yt-dlp" -o /usr/local/bin/yt-dlp \
    && echo "${YTDLP_SHA256}  /usr/local/bin/yt-dlp" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/yt-dlp
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip check
COPY main.py bigo.py bigo_recorder.py network_policy.py index.html egress-domains.txt ./
RUN mkdir -p /recordings && groupadd -g 999 streamrec \
    && useradd --system --uid 999 --gid 999 --home-dir /app --shell /usr/sbin/nologin streamrec \
    && chown streamrec:streamrec /recordings
USER streamrec
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf --noproxy '*' http://localhost:8080/api/health || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--limit-concurrency", "20", "--timeout-keep-alive", "10"]
