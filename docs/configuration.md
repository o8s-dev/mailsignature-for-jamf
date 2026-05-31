# Configuration

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes (production) | Flask session secret. Generate with `openssl rand -hex 32`. The app refuses to start in production without it. |

Set it in `.env` (Docker reads it via `docker-compose.yml`) or in the process environment.

## Data locations

| Path | Contents |
|---|---|
| `data/signatures.db` | SQLite database (organizations, members, users, settings) |
| `static/logos/` | Uploaded organization logos |

Both are mounted as volumes in Docker, so they survive rebuilds. Back them up (see the
[README](../README.md#backups)).

## Signature templates

Each organization picks a template (in the organization form). Optional fields auto-hide
when empty, so one template covers "with/without logo" etc.

| Template | Description |
|---|---|
| `classic` | Default — logo at the bottom, note, disclaimer |
| `minimal` | Text only |
| `logo` | Logo left, text right |
| `compact` | A few lines only |
| `accent` | Accent bar in the organization's **brand color** (set via the color field) |
| `custom` | **Your own HTML** with placeholders (see below) |

### Custom HTML placeholders

Choose the **Custom HTML** template and write your own HTML in the organization form.
The following placeholders are replaced with the member's data:

```
{{name}}                full name (title + first + last)
{{title}}               title / academic degree
{{first_name}} {{last_name}}
{{email}}
{{phone}}               member phone, falls back to the organization phone
{{organization}}        organization name
{{organization_phone}}
{{address1}} {{address2}}
{{availability}}
{{custom_note}}         organization note
{{disclaimer}}
{{logo}}                a ready-made <img> tag (empty if no logo)
```

**Safety:** custom HTML is **not** run through a template engine (no
server-side template injection — `{{ config }}` and the like are *not* evaluated). Only
the placeholders above are substituted, and inserted values are HTML-escaped. Build
signatures email-safe: use table layouts and inline styles (Apple Mail renders those most
reliably).

## CSV import format

Members can be bulk-imported on the **Import** screen. A template is downloadable there.

- First row must be the **header**.
- Required columns: `email`, `first_name`.
- Optional: `title`, `last_name`, `phone`, `availability`, `sig_label`.
- Delimiter selectable (comma / semicolon / tab); UTF-8 recommended.
- Existing email → the member is **updated**; new email → **created**.

Example:

```csv
email,title,first_name,last_name,phone,availability,sig_label
max.muster@example.com,,Max,Muster,+41 44 000 00 00,"Mon-Fri 08:00-17:00",
erika.beispiel@example.com,Dr.,Erika,Beispiel,,,
```

## API key

The signature API is protected by a key that is baked into the downloaded Jamf script.
You can **regenerate** it on the Deploy screen — afterwards re-download the script and
update it in Jamf (the new key invalidates the old script).

## Users & access

- The **first** account created (on first visit) is an admin.
- Admins can create more users; the `admin` flag governs **user management** only.
- Note: every logged-in user can see and manage **all** organizations (no per-tenant
  isolation — see the README's "Good to know").

## Language

The UI is bilingual (DE/EN) and follows the browser/user preference. Default locale is
German; switch via the language menu.
