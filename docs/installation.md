# Installation

This guide sets up MailSign on a server with Docker and puts it behind HTTPS.

## 1. Prerequisites

- A Linux server with **Docker** and the **Docker Compose** plugin.
- A **domain name** pointing at the server (recommended, for HTTPS).
- The Macs you deploy to must be able to reach this server.

> No Docker? You can also run it directly with Python 3.12+ — see
> [Running without Docker](#running-without-docker) at the bottom.

## 2. Get the code

```bash
git clone https://github.com/o8s-dev/mailsignature-for-jamf.git
cd mailsignature-for-jamf
```

## 3. Set the secret key

The app refuses to start in production without a `SECRET_KEY` (it signs the login
session). Create one:

```bash
cp .env.example .env
# put a random value into .env, e.g. generate one with:
openssl rand -hex 32
```

`.env` is git-ignored and stays only on your server.

## 4. Start it

```bash
docker compose up -d --build
```

The app listens on **port 5050**. The SQLite database is created and migrated
automatically on first start.

Open `http://SERVER-IP:5050` in a browser — the **first visit** lets you create the
initial **admin account**. Choose a strong password; the page is reachable by anyone who
can reach the server.

## 5. HTTPS with a reverse proxy (recommended)

For production, terminate HTTPS with a reverse proxy and don't expose port 5050 directly.
[Caddy](https://caddyserver.com/) gets you automatic Let's Encrypt certificates with a
two-line config.

**Option A — Caddy in front (simplest):** stop publishing 5050 and let Caddy reach the
container over a shared Docker network. Example `Caddyfile`:

```
signatures.example.com {
    reverse_proxy mailsign:5050
}
```

A minimal Caddy + MailSign `compose` looks like this (one network, no public app port):

```yaml
services:
  mailsign:
    build: .
    container_name: mailsign
    restart: unless-stopped
    volumes:
      - ./data:/app/data
      - ./static/logos:/app/static/logos
    environment:
      - SECRET_KEY=${SECRET_KEY}
    networks: [web]

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy-data:/data
    networks: [web]

networks: { web: {} }
volumes: { caddy-data: {} }
```

```bash
docker compose up -d --build
```

Caddy fetches the certificate automatically. Your instance is now at
`https://signatures.example.com`.

**Option B — existing nginx/Traefik:** just reverse-proxy your hostname to the container's
port 5050; keep the default `docker-compose.yml` (which publishes 5050) bound to localhost.

## 6. DNS

Point an A record at your server, e.g. `signatures` → your server IP. Verify:

```bash
dig +short signatures.example.com
```

## Updating

```bash
git pull
docker compose up -d --build
```

`data/`, `static/logos/` and `.env` are preserved; the database migrates automatically.

## Backups

```bash
tar czf mailsign-backup-$(date +%F).tar.gz data static/logos
```

Store the archive off-box. If your host offers VM snapshots, enable them too.

## Running without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY=$(openssl rand -hex 32)
# compile translations once:
pybabel compile -d translations
gunicorn -b 0.0.0.0:5050 -w 2 app:app
```

## Troubleshooting

- **"SECRET_KEY environment variable is required in production"** — set `SECRET_KEY` in `.env` (Docker) or the environment (bare metal).
- **Certificate not issued** — make sure ports 80/443 are open and DNS resolves to the server *before* the proxy starts.
- **Can't reach from a Mac** — the client needs network access to the server URL shown on the Deploy page.
