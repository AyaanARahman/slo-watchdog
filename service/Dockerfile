# syntax=docker/dockerfile:1

# ---------- build stage ----------
# Dependencies are resolved and compiled here, then only the finished virtualenv
# crosses into the runtime image. Build tooling, pip's cache, and the package
# metadata never reach the final layer, which keeps both the image and its
# vulnerability surface small.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# ---------- runtime stage ----------
FROM python:3.12-slim AS runtime

# A numeric, non-root UID. Kubernetes `runAsNonRoot` verifies the *numeric* id,
# so a name-only USER would still fail admission.
ARG UID=10001
RUN useradd --uid ${UID} --create-home --shell /usr/sbin/nologin appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# PYTHONDONTWRITEBYTECODE matters more than usual here: the container runs with a
# read-only root filesystem, so an attempt to write __pycache__ would fail.
COPY --from=builder --chown=root:root /opt/venv /opt/venv

USER ${UID}
WORKDIR /home/appuser
EXPOSE 8000

# Kubernetes uses its own probes against /healthz and /readyz; this exists so the
# image is also correct under a plain `docker run`.
HEALTHCHECK --interval=10s --timeout=2s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"

# Single worker on purpose. Chaos config and the in-memory store live in process
# memory, so a second worker would serve a different store and a different error
# rate. Scale with replicas, not workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
