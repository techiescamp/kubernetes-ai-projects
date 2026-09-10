FROM python:3.12-slim AS base

WORKDIR /app

RUN useradd -u 10001 -m appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agents.py bedrock_clients.py k8s_tools.py main.py usage_tracker.py \
     checkpointer.py logging_config.py metrics.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
