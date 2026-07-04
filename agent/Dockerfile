# Build from the REPO ROOT so scaffold.py + template/ are in context:
#   docker build -f agent/Dockerfile -t us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/deploy-agent:latest .
FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq git openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The engine (reused unchanged from the CLI path)
COPY scaffold.py /app/scaffold.py
COPY template /app/template
# The Slack agent
COPY agent/app.py /app/app.py
COPY agent/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV QH_DEPLOYMENT_PATH=/work/qh-deployment SCAFFOLD=/app/scaffold.py
ENTRYPOINT ["/app/entrypoint.sh"]
