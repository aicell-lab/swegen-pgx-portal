# SweGen PGx Portal — public Safe Colab service.
#
# Self-contained build: nothing outside this repo is needed.
# The dataset + sensitivity contract live in ../data/ and are baked into
# the image at /data so the pod has nothing to fetch at startup.
#
# Build (must be linux/amd64 — KTH cluster is x86):
#     docker buildx build --platform linux/amd64 \
#         -f portal/Dockerfile \
#         -t <user>/swegen-pgx-portal:<tag> \
#         --push .
#
# The build context is the **repo root** so we can COPY both portal/ and data/.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HYPHA_SERVER_URL=https://hypha.aicell.io \
    PORTAL_DATA_DIR=/data \
    PORTAL_WORK_DIR=/work \
    PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (FastAPI, Jupyter, scientific stack)
COPY portal/requirements.txt /tmp/portal-requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/portal-requirements.txt

# Portal source
COPY portal /app/portal

# Bake the dataset + sensitivity contract into the image.
RUN mkdir -p /data /work
COPY data /data

# Non-root user
RUN useradd -u 1000 -m portal \
 && chown -R portal:portal /app /data /work
USER portal

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "portal.app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
