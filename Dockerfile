# Recovery Desk always-on worker (Option B). Screening only; orders stay OFF.
#
#   docker build -t recovery-desk .
#   docker run -d --restart=unless-stopped --read-only --tmpfs /tmp \
#     --cap-drop=ALL --security-opt no-new-privileges --env-file /etc/recovery-desk.env recovery-desk
#
# The env file holds the desk's own secrets (see docs/RECOVERY_DESK_CLOUD.md);
# keep it chmod 600 and out of git. The container needs no inbound ports.
FROM python:3.11-slim
RUN useradd --create-home --uid 10001 desk
WORKDIR /app
COPY requirements-worker.txt .
RUN pip install --no-cache-dir -r requirements-worker.txt
COPY recovery/ recovery/
COPY scripts/run_recovery_desk.py scripts/
USER desk
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RECOVERY_ROLE=worker
CMD ["python", "scripts/run_recovery_desk.py", "--loop"]
