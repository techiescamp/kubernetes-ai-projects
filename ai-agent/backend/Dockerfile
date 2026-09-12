FROM python:3.12-slim AS base

WORKDIR /app

# Don't write .pyc cache files (pointless and noisy under readOnlyRootFilesystem in k8s), and
# flush stdout/stderr immediately so `kubectl logs` shows output in real time, not buffered.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN useradd -u 10001 -m appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy every module rather than an explicit filename list: the list silently went stale the first
# time a new module was added (conversation_store.py built fine and then crash-looped at import
# with ModuleNotFoundError, because it simply wasn't in the COPY). .dockerignore keeps tests,
# caches and local .env out of the image.
COPY *.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
