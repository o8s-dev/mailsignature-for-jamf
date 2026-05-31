# MailSign for Jamf

**Centrally manage Apple Mail signatures for your whole fleet and deploy them with Jamf Pro — self-hosted, no third-party cloud.**

MailSign is a small self-hosted web app that manages email signatures for multiple organizations and their members, and ships a ready-to-run Jamf deployment script that writes the correct `.mailsignature` files onto each Mac. You keep full control of your data — it runs on your own server.

<!-- TODO: add a screenshot of the dashboard + signature preview -->

## Why

Most Apple Mail signature solutions are paid SaaS products that route your staff data through a third party. If you run Jamf Pro, you don't need that — you need a place to manage signatures and a script that deploys them reliably. MailSign is exactly that, on infrastructure you control.

## Features

- **Multi-organization management** — manage signatures for several organizations, each with its own name, address, logo, custom note, and confidentiality disclaimer.
- **Member management** — add members individually or bulk-import via CSV.
- **Live preview** — see each signature rendered as it will appear in Apple Mail.
- **Jamf deployment** — download a self-contained bash script (uses only built-in macOS tools — no Python, no Command Line Tools required on the client) and run it as a Jamf policy.
- **PPPC profile generator** — generates the configuration profile needed to allow the deployment script to control Apple Mail via AppleScript.
- **API-key protected endpoints** — clients fetch their signatures over a key-protected API.
- **Idempotent & deterministic** — each signature gets a stable UUID; re-running the policy updates instead of duplicating.

## Tech stack

Python / Flask, SQLite (zero-config, file-based), served by gunicorn, packaged with Docker. Data persists in two folders: `data/` (the SQLite database) and `static/logos/` (uploaded logos).

## Quick start (Docker)

```bash
# 1. Clone
git clone https://git.macops.ch/Administrator/mailsign-for-jamf.git
cd mailsign-for-jamf

# 2. Configure the secret key
cp .env.example .env
# edit .env and set SECRET_KEY to a random value, e.g. from:
openssl rand -hex 32

# 3. Build & run
docker compose up -d --build
```

The app is now on port 5050. Open it in your browser — on first visit you'll be prompted to create the initial admin account.

For public/production use, put a reverse proxy (e.g. Caddy) in front for automatic HTTPS, and point the app at a real domain.

## Usage

1. Create an **organization** (name, address, logo, optional note, disclaimer).
2. Add **members** — individually or by CSV import (a template is available in the import screen).
3. Check each signature in the **preview**.
4. Go to **Deploy**: download the PPPC profile and the Jamf script.
5. In Jamf Pro: upload the PPPC profile as a configuration profile, add the script, and attach it to a policy scoped to your Macs.
6. The script detects the logged-in user, fetches their signature from the server, and writes it into Apple Mail.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes (production) | Flask session secret. Generate with `openssl rand -hex 32`. The app refuses to start in production without it. |

## Backups

Everything lives in two folders — back them up and you're covered:

```bash
tar czf mailsign-backup-$(date +%F).tar.gz data static/logos
```

## Security notes

- Always run behind HTTPS in production (reverse proxy).
- Keep `SECRET_KEY` secret and out of version control (`.env` is git-ignored).
- The signature API is protected by an API key; regenerate it from the Deploy screen if needed.
- If you operate this as a service for other organizations and store their personal data, make sure you comply with applicable data-protection law (e.g. Swiss FADP / GDPR).

## License

Released under the [MIT License](LICENSE). Use it freely, including commercially.
