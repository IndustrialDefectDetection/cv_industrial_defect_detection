# Docker deployment

The Compose stacks are intended for a local portfolio/demo environment. Every
published port is bound to `127.0.0.1`; do not remove that restriction without
placing authenticated TLS termination in front of the services.
Use a current Docker Compose v2 release; legacy `docker-compose` v1 does not
support the environment-backed secrets used here.

MLflow, Streamlit, and Prometheus do not provide an application login in this
stack. Loopback binding prevents remote network access, but other trusted local
processes can still reach them. Do not use these Compose files on a shared host
without adding an authenticated reverse proxy.

## Start the API and MLflow

Create a unique service token, then start the small stack:

```bash
export MES_INTERNAL_API_TOKEN="$(openssl rand -hex 32)"
docker compose -f deployment/docker-compose.yml up --build
```

The token is mounted into the API container as a Compose secret. It must be at
least 32 and at most 512 characters, using only letters, numbers, underscores,
or hyphens. Do not commit it to this repository or paste it into the Compose
file.

The bundled PyTorch model is executable serialized data. Both the API and
Streamlit verify it against the reviewed `MODEL_SHA256` before deserialization.
When retraining, calculate the new SHA-256 digest, review the model source, and
update `deployment/model_integrity.py` deliberately.

- API and public health check: http://localhost:8080
- API documentation: http://localhost:8080/docs
- MLflow: http://localhost:5000

## Start the full local stack

Grafana also requires a unique admin password:

```bash
export MES_INTERNAL_API_TOKEN="$(openssl rand -hex 32)"
export GRAFANA_ADMIN_PASSWORD="$(openssl rand -base64 32)"
export GRAFANA_SECRET_KEY="$(openssl rand -hex 32)"
docker compose -f deployment/docker-compose.full.yml up --build
```

The full stack additionally exposes these loopback-only URLs:

- Streamlit: http://localhost:8501
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000

The default Grafana username is `admin`; the password is the value supplied in
`GRAFANA_ADMIN_PASSWORD`. Override the username with
`GRAFANA_ADMIN_USER` if desired. `GRAFANA_SECRET_KEY` encrypts credentials
stored by Grafana and must remain stable after the first start. Prometheus
receives the API token through a read-only secret file when it scrapes
`/metrics`.

## Call the API

The root and `/health` endpoints are public. Prediction, model information, and
metrics endpoints require the internal token header.

```bash
curl \
  --header "X-MES-Internal-Token: ${MES_INTERNAL_API_TOKEN}" \
  --form "file=@data/dataset/images/test/patches_1.jpg" \
  "http://localhost:8080/predict?confidence=0.25"

curl \
  --header "X-MES-Internal-Token: ${MES_INTERNAL_API_TOKEN}" \
  --form "files=@image1.jpg" \
  --form "files=@image2.jpg" \
  "http://localhost:8080/batch-predict?confidence=0.30"

curl \
  --header "X-MES-Internal-Token: ${MES_INTERNAL_API_TOKEN}" \
  http://localhost:8080/model-info

curl http://localhost:8080/health
```

PowerShell example:

```powershell
$headers = @{ "X-MES-Internal-Token" = $env:MES_INTERNAL_API_TOKEN }
$image = Get-Item "data\dataset\images\test\patches_1.jpg"
Invoke-RestMethod `
  -Uri "http://localhost:8080/predict?confidence=0.25" `
  -Method Post `
  -Headers $headers `
  -Form @{ file = $image }
```

## Container management

```bash
docker compose -f deployment/docker-compose.yml up -d
docker compose -f deployment/docker-compose.yml logs -f
docker compose -f deployment/docker-compose.yml restart defect-api
docker compose -f deployment/docker-compose.yml down
```

MLflow, Prometheus, and Grafana data are stored in named Docker volumes and
survive a normal `down`. Running `down --volumes` permanently deletes those
volumes.

The images run as non-root users with dropped capabilities, a read-only root
filesystem, bounded process counts, and digest-pinned base/runtime images.
Top-level Python packages are also pinned to exact versions.
The build context is allow-listed by `.dockerignore`, so local environments,
datasets, and other unrelated files are not sent to the Docker daemon.
