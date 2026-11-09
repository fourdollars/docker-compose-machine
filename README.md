# docker-compose-machine

[![GitHub](https://img.shields.io/badge/GitHub-fourdollars/docker--compose--machine-blue.svg)](https://github.com/fourdollars/docker-compose-machine)
[![CI](https://github.com/fourdollars/docker-compose-machine/actions/workflows/test.yaml/badge.svg)](https://github.com/fourdollars/docker-compose-machine/actions/workflows/test.yaml)
[![Charmhub](https://charmhub.io/docker-compose-machine/badge.svg)](https://charmhub.io/docker-compose-machine)

A minimal Juju machine charm for rapidly deploying arbitrary `docker-compose.yml` services inside a Juju environment.

> **⚠️ Disclaimer (PoC Only — Not for Production):**  
> This charm is strictly intended for **rapid Proof-of-Concept (PoC) validation and testing** of `docker-compose` services inside a Juju charm. It intentionally omits Juju relations, high-availability clustering, backups, secret rotation, and production-grade lifecycle management. **Do NOT use this charm in production environments.**

---

## Configuration Options

| Option | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `raw` | `string` | `""` | Inline `docker-compose.yml` content (mutually exclusive with `url`). |
| `url` | `string` | `""` | HTTP/HTTPS URL pointing to a remote `docker-compose.yml` file or Git repository (supports Git URLs like `https://github.com/org/repo.git#subpath` and GitHub raw/blob/tree URLs, mutually exclusive with `raw`). |
| `env` | `string` | `""` | Optional multi-line `KEY=VALUE` `.env` content written alongside `compose.yaml` to substitute `${VAR}` variables in `docker-compose.yml`. |
| `tailscale-secret` | `string` | `""` | Optional Juju Secret URI (`secret:<id>`) configuring Tailscale on-demand. When unset, Tailscale is not installed. Supported secret keys: `auth-key` (or `authkey`), `hostname`, `domain`, `web-port`, `ports`. |

Published service ports (`services.*.ports`, excluding `127.0.0.1`) are automatically detected via `docker compose config --format json` and registered with Juju (`open-port`), so `juju expose <app>` works out of the box.

---

## Usage Examples

### 1. Deploy with Inline `raw` Config (Concourse CI)

```yaml
# config.yaml
concourse-ci:
  raw: |-
    services:
      concourse-db:
        image: postgres
        environment:
          POSTGRES_DB: concourse
          POSTGRES_PASSWORD: concourse_pass
          POSTGRES_USER: concourse_user
          PGDATA: /database
          POSTGRES_HOST_AUTH_METHOD: trust #for healthcheck
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U concourse_user -d concourse"]
          interval: 3s
          timeout: 3s
          retries: 5

      concourse:
        image: concourse/concourse
        command: quickstart
        privileged: true
        cgroup: host
        depends_on:
          concourse-db:
            condition: service_healthy
        ports: ["8080:8080"]
        environment:
          CONCOURSE_POSTGRES_HOST: concourse-db
          CONCOURSE_POSTGRES_USER: concourse_user
          CONCOURSE_POSTGRES_PASSWORD: concourse_pass
          CONCOURSE_POSTGRES_DATABASE: concourse
          CONCOURSE_EXTERNAL_URL: http://localhost:8080
          CONCOURSE_ADD_LOCAL_USER: test:test
          CONCOURSE_MAIN_TEAM_LOCAL_USER: test
          # instead of relying on the default "detect"
          CONCOURSE_WORKER_BAGGAGECLAIM_DRIVER: overlay
          CONCOURSE_CLIENT_SECRET: Y29uY291cnNlLXdlYgo=
          CONCOURSE_TSA_CLIENT_SECRET: Y29uY291cnNlLXdvcmtlcgo=
          CONCOURSE_CLUSTER_NAME: tutorial
          CONCOURSE_WORKER_CONTAINERD_DNS_SERVER: "8.8.8.8"
          CONCOURSE_ENABLE_RESOURCE_CAUSALITY: "true"
```

```bash
juju deploy docker-compose-machine concourse-ci --config ./config.yaml
juju expose concourse-ci
```

### 2. Deploy from Remote `url` (Concourse CI Quickstart)

```yaml
# concourse-url.yaml
concourse-ci:
  url: https://concourse-ci.org/docker-compose.yml
```

```bash
juju deploy docker-compose-machine concourse-ci --config ./concourse-url.yaml
# Or directly via CLI flag:
juju deploy docker-compose-machine concourse-ci --config url=https://concourse-ci.org/docker-compose.yml
```

### 3. Deploy from Remote `url` with `env` Variables (Temporal Server)

```yaml
# examples/temporal.yaml
temporal:
  url: https://raw.githubusercontent.com/temporalio/temporal-server-operations/refs/heads/main/compose/docker-compose.yml
  env: |-
    POSTGRESQL_VERSION=16
    ELASTICSEARCH_VERSION=7.17.27
    TEMPORAL_VERSION=1.32.0
    TEMPORAL_ADMINTOOLS_VERSION=1.32.0
    TEMPORAL_UI_VERSION=2.54.1
```

```bash
juju deploy docker-compose-machine temporal --config ./examples/temporal.yaml
juju expose temporal
```

### 4. Deploy Hindsight Agent Memory via Inline `raw` Config

```yaml
# examples/hindsight.yaml
hindsight:
  raw: |-
    services:
      hindsight:
        image: ghcr.io/vectorize-io/hindsight:latest
        container_name: hindsight
        restart: unless-stopped
        ports:
          - "8888:8888"
          - "9999:9999"
        environment:
          - HINDSIGHT_API_LLM_API_KEY=$OPENAI_API_KEY
        volumes:
          - hindsight-data:/home/hindsight/.pg0

    volumes:
      hindsight-data:
  env: |-
    OPENAI_API_KEY=sk-dummy
```

```bash
juju deploy docker-compose-machine hindsight --config ./examples/hindsight.yaml
juju expose hindsight
```

### 5. Tailscale Integration with Juju Secrets & Multi-Port Serve

Tailscale is installed **on-demand** only when `tailscale-secret` is configured (zero overhead by default). The secret can contain credentials, custom hostnames, domains, and port mappings.

#### Secret Keys

| Key | Required | Description |
| :--- | :--- | :--- |
| `auth-key` (or `authkey`) | No | Tailscale auth key (e.g. `tskey-auth-...`). If omitted, authenticate via `tailscale-login` action or SSH. |
| `hostname` | No | Node hostname within your Tailnet. |
| `domain` | No | Domain name (injected as `DOMAIN` into `.env`). |
| `web-port` | No | Primary web UI port to expose via Tailscale HTTPS 443 with automatic TLS. |
| `ports` | No | Comma-separated TCP ports to proxy. If omitted, remaining compose ports are auto-detected. |

#### Automated Authentication (Headless with Auth Key)

Example for Temporal (`web-port: 8080`, backend TCP ports `5432, 7233, 9200`):

```bash
# 1. Create the secret in Juju
juju add-secret ts-secret \
  auth-key=tskey-auth-k123456789... \
  hostname=temporal \
  web-port=8080 \
  ports=5432,7233,9200

# 2. Grant access to the deployed charm
juju grant-secret ts-secret temporal

# 3. Apply the secret to the charm
juju config temporal tailscale-secret=secret:cm3v...
```

#### Interactive Authentication (Without Auth Key)

If you prefer interactive browser login without generating long-lived auth keys:

```bash
# 1. Create secret without auth-key
juju add-secret ts-secret hostname=hindsight web-port=8888
juju grant-secret ts-secret hindsight
juju config hindsight tailscale-secret=secret:cm3v...

# 2. Tailscale is installed on-demand; fetch the login URL directly via action (no SSH required):
juju run hindsight/0 tailscale-login

# 3. Output provides the interactive authentication URL:
#    results:
#      login-url: https://login.tailscale.com/a/abcdef123456
#      status: needs_login

# 4. Open the URL in your browser to approve the machine!
```

You can also check Tailscale connection state anytime:

```bash
juju run hindsight/0 tailscale-status
```


---

## Development

This project uses [`uv`](https://docs.astral.sh/uv/) and [`ruff`](https://docs.astral.sh/ruff/):

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

---

## License

[MIT License](LICENSE)
