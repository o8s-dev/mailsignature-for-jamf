import os
import io
import csv
import json
import uuid
import hmac
import secrets
import quopri
import sqlite3
import base64
from pathlib import Path
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse, urljoin
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, send_file, abort, Response, session
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import escape
from flask_babel import Babel, gettext as _

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "signatures.db"
LOGOS_DIR = BASE_DIR / "static" / "logos"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "tiff", "tif"}

# Optionaler Hinweistext – standardmässig leer, vom Nutzer frei befüllbar.
DEFAULT_CUSTOM_NOTE = ""

# Neutrale, generische Vertraulichkeits-Note (DE + EN). Frei anpassbar pro Organisation.
DEFAULT_DISCLAIMER = (
    "Confidentiality Note: This message and any attachments are intended only for the use "
    "of the named recipient(s) and may contain confidential and/or proprietary information. "
    "If you are not the intended recipient, please notify the sender and delete this message. "
    "Any unauthorized use of the information contained in this message is prohibited.\n\n"
    "Vertraulichkeitshinweis: Diese Nachricht und allfällige Anhänge sind ausschliesslich für "
    "die genannten Empfänger bestimmt und können vertrauliche und/oder geschützte Informationen "
    "enthalten. Wenn Sie nicht der beabsichtigte Empfänger sind, benachrichtigen Sie bitte den "
    "Absender und löschen Sie diese Nachricht. Jede unbefugte Nutzung der enthaltenen "
    "Informationen ist untersagt."
)

app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    # Kein SECRET_KEY gesetzt: im Betrieb ein Fehler. Für lokale Entwicklung
    # wird ein flüchtiger Zufallswert verwendet (Sessions überleben keinen Neustart).
    import sys as _sys
    if os.environ.get("FLASK_ENV") == "development" or __name__ == "__main__":
        _secret = secrets.token_hex(32)
        print("WARNUNG: SECRET_KEY nicht gesetzt – flüchtiger Schlüssel für Entwicklung.", file=_sys.stderr)
    else:
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
app.secret_key = _secret
app.config["MAX_CONTENT_LENGTH"]    = 2 * 1024 * 1024   # 2 MB Upload-Limit
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"]  = "Lax"

# ---------------------------------------------------------------------------
# Internationalisierung (Flask-Babel)
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES = ["de", "en"]
app.config["BABEL_DEFAULT_LOCALE"] = "de"
app.config["BABEL_TRANSLATION_DIRECTORIES"] = str(BASE_DIR / "translations")


def select_locale():
    """Sprachwahl in dieser Priorität:
    1. Eingeloggt + Konto-Präferenz gesetzt → diese Sprache
    2. Cookie 'lang' (z.B. vor dem Login per Umschalter gesetzt)
    3. Browser-Sprache (Accept-Language)
    4. Default (de)
    """
    # 1. Konto-Präferenz (nur wenn eingeloggt)
    if session.get("user_id"):
        pref = session.get("lang")
        if pref in SUPPORTED_LANGUAGES:
            return pref
    # 2. Cookie
    cookie_lang = request.cookies.get("lang")
    if cookie_lang in SUPPORTED_LANGUAGES:
        return cookie_lang
    # 3. Browser
    best = request.accept_languages.best_match(SUPPORTED_LANGUAGES)
    if best:
        return best
    # 4. Default
    return app.config["BABEL_DEFAULT_LOCALE"]


babel = Babel(app, locale_selector=select_locale)


@app.context_processor
def inject_locale():
    """Stellt aktuelle Sprache + Liste in allen Templates bereit."""
    return {
        "current_lang": select_locale(),
        "supported_languages": SUPPORTED_LANGUAGES,
    }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            address1    TEXT,
            address2    TEXT,
            phone       TEXT,
            logo        TEXT,
            custom_note TEXT,
            disclaimer  TEXT,
            template    TEXT DEFAULT 'classic',
            accent_color TEXT,
            custom_html TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER DEFAULT 0,
            language      TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS employees (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            email       TEXT NOT NULL COLLATE NOCASE,
            title       TEXT,
            first_name  TEXT NOT NULL,
            last_name   TEXT,
            phone       TEXT,
            availability TEXT,
            sig_label   TEXT,
            sig_uuid    TEXT,
            active      INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)


def migrate_db():
    """Bring existing databases up to current schema."""
    with get_db() as conn:
        cols = conn.execute("PRAGMA table_info(employees)").fetchall()
        col_names = {c["name"] for c in cols}

        # users: language-Spalte für UI-Sprachpräferenz
        user_cols = {c["name"] for c in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "language" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN language TEXT")

        # organizations: template-Spalte für Signatur-Vorlagenauswahl
        org_cols = {c["name"] for c in conn.execute("PRAGMA table_info(organizations)").fetchall()}
        if "template" not in org_cols:
            conn.execute("ALTER TABLE organizations ADD COLUMN template TEXT DEFAULT 'classic'")
        if "accent_color" not in org_cols:
            conn.execute("ALTER TABLE organizations ADD COLUMN accent_color TEXT")
        if "custom_html" not in org_cols:
            conn.execute("ALTER TABLE organizations ADD COLUMN custom_html TEXT")

        # Step 1: Add missing columns via ALTER TABLE
        for col, definition in [
            ("availability", "TEXT"),
            ("sig_label",    "TEXT"),
            ("sig_uuid",     "TEXT"),
        ]:
            if col not in col_names:
                conn.execute(f"ALTER TABLE employees ADD COLUMN {col} {definition}")
                col_names.add(col)

        # Step 2: Rebuild table if email still has a UNIQUE constraint
        # (legacy schema or old migration result)
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='employees'"
        ).fetchone()
        table_sql = (table_row["sql"] or "") if table_row else ""
        # Detect inline UNIQUE on email or a separate UNIQUE constraint
        import re as _re
        email_unique = bool(
            _re.search(r"\bemail\b[^,)]*\bUNIQUE\b", table_sql, _re.IGNORECASE) or
            _re.search(r"\bUNIQUE\b\s*\([^)]*\bemail\b", table_sql, _re.IGNORECASE)
        )
        # Also handle the very old schema where last_name was NOT NULL
        last_name_notnull = next(
            (c["notnull"] for c in cols if c["name"] == "last_name"), 0
        )
        if email_unique or last_name_notnull:
            conn.executescript("""
                CREATE TABLE employees_rebuilt (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    email       TEXT NOT NULL COLLATE NOCASE,
                    title       TEXT,
                    first_name  TEXT NOT NULL,
                    last_name   TEXT,
                    phone       TEXT,
                    availability TEXT,
                    sig_label   TEXT,
                    sig_uuid    TEXT,
                    active      INTEGER DEFAULT 1,
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO employees_rebuilt
                    (id, organization_id, email, title, first_name, last_name,
                     phone, availability, sig_label, sig_uuid, active, created_at, updated_at)
                SELECT
                    id, organization_id, email, title, first_name, last_name,
                    phone, availability, sig_label, sig_uuid, active, created_at, updated_at
                FROM employees;
                DROP TABLE employees;
                ALTER TABLE employees_rebuilt RENAME TO employees;
            """)

        # Step 3: Populate sig_uuid for rows that don't have one yet
        rows = conn.execute(
            "SELECT id, email FROM employees WHERE sig_uuid IS NULL OR sig_uuid = ''"
        ).fetchall()
        for row in rows:
            generated = str(uuid.uuid5(uuid.NAMESPACE_DNS, row["email"].lower().strip()))
            conn.execute("UPDATE employees SET sig_uuid=? WHERE id=?", (generated, row["id"]))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def sig_uuid_for(email):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, email.lower().strip()))


# Signatur-Vorlagen: Schlüssel → Template-Datei. Auswahl erfolgt pro Organisation.
SIGNATURE_TEMPLATES = {
    "classic": "signature_body.html",
    "minimal": "signatures/minimal.html",
    "logo":    "signatures/logo.html",
    "compact": "signatures/compact.html",
    "accent":  "signatures/accent.html",
}
DEFAULT_SIGNATURE_TEMPLATE = "classic"
DEFAULT_ACCENT_COLOR = "#2563EB"

# "custom" = vom Power-User selbst geschriebenes HTML mit Platzhaltern.
# Wird NICHT als Jinja gerendert (SSTI-Schutz), sondern per fester
# Platzhalter-Ersetzung mit escapten Werten – siehe render_custom_html().
CUSTOM_TEMPLATE = "custom"
VALID_TEMPLATES = set(SIGNATURE_TEMPLATES) | {CUSTOM_TEMPLATE}

# Verfügbare Platzhalter im „Eigenes HTML"-Modus (für UI-Hinweis & Doku).
CUSTOM_PLACEHOLDERS = [
    "name", "title", "first_name", "last_name", "email", "phone",
    "organization", "organization_phone", "address1", "address2",
    "availability", "custom_note", "disclaimer", "logo",
]


def normalize_template(name):
    """Gültigen Vorlagen-Schlüssel zurückgeben (Fallback: Standard)."""
    return name if name in VALID_TEMPLATES else DEFAULT_SIGNATURE_TEMPLATE


def normalize_color(value):
    """Hex-Farbe (#RRGGBB) validieren, sonst None — schützt vor CSS-Injection."""
    v = (value or "").strip()
    if len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
        return v
    return None


def build_signature_payload(emp, organization):
    logo_b64 = None
    if organization["logo"]:
        logo_path = LOGOS_DIR / organization["logo"]
        if logo_path.exists():
            with open(logo_path, "rb") as f:
                raw = f.read()
            ext = organization["logo"].rsplit(".", 1)[-1].lower()
            mime = "image/svg+xml" if ext == "svg" else f"image/{ext}"
            logo_b64 = f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    last_name = (emp["last_name"] or "").strip() if emp["last_name"] else ""
    sig_name_base = " ".join(
        part for part in [emp["title"], emp["first_name"], last_name] if part
    ).strip()

    # Append label to distinguish multiple signatures for the same account
    try:
        sig_label = (emp["sig_label"] or "").strip()
    except (IndexError, KeyError):
        sig_label = ""
    sig_name = f"{sig_name_base} ({sig_label})" if sig_label else sig_name_base

    try:
        availability = emp["availability"] or ""
    except (IndexError, KeyError):
        availability = ""

    # Use stored sig_uuid; fall back to deterministic uuid5 for legacy rows
    try:
        stored_uuid = emp["sig_uuid"] or ""
    except (IndexError, KeyError):
        stored_uuid = ""
    sig_uuid_val = stored_uuid or sig_uuid_for(emp["email"])

    try:
        template_name = normalize_template(organization["template"] or DEFAULT_SIGNATURE_TEMPLATE)
    except (IndexError, KeyError):
        template_name = DEFAULT_SIGNATURE_TEMPLATE

    try:
        accent_color = normalize_color(organization["accent_color"]) or DEFAULT_ACCENT_COLOR
    except (IndexError, KeyError):
        accent_color = DEFAULT_ACCENT_COLOR

    try:
        custom_html = organization["custom_html"] or ""
    except (IndexError, KeyError):
        custom_html = ""

    return {
        "sig_uuid": sig_uuid_val,
        "template": template_name,
        "accent_color": accent_color,
        "custom_html": custom_html,
        "sig_name": sig_name,
        "title": emp["title"] or "",
        "first_name": emp["first_name"],
        "last_name": last_name,
        "email": emp["email"],
        "phone": emp["phone"] or "",
        "availability": availability,
        "organization_name": organization["name"],
        "address1": organization["address1"] or "",
        "address2": organization["address2"] or "",
        "organization_phone": organization["phone"] or "",
        "custom_note": organization["custom_note"] or DEFAULT_CUSTOM_NOTE,
        "disclaimer": organization["disclaimer"] or DEFAULT_DISCLAIMER,
        "logo_b64": logo_b64,
    }


def render_custom_html(payload):
    """Eigenes HTML des Power-Users mit Platzhaltern füllen.

    Sicher: KEIN Jinja auf User-Input (kein SSTI). Feste Platzhalter werden per
    str.replace ersetzt, eingesetzte Werte werden HTML-escaped (kein XML/HTML aus
    Mitarbeiterdaten). `{{logo}}` ist ein von uns erzeugtes <img>-Tag.
    """
    name = " ".join(
        p for p in [payload.get("title", ""), payload.get("first_name", ""),
                    payload.get("last_name", "")] if p
    ).strip()
    logo_b64 = payload.get("logo_b64")
    logo_tag = (
        f'<img src="{logo_b64}" alt="{escape(payload.get("organization_name", ""))}" '
        f'style="max-height:70px; max-width:280px; border:0;">'
    ) if logo_b64 else ""

    values = {
        "name": name,
        "title": payload.get("title", ""),
        "first_name": payload.get("first_name", ""),
        "last_name": payload.get("last_name", ""),
        "email": payload.get("email", ""),
        "phone": payload.get("phone", "") or payload.get("organization_phone", ""),
        "organization": payload.get("organization_name", ""),
        "organization_phone": payload.get("organization_phone", ""),
        "address1": payload.get("address1", ""),
        "address2": payload.get("address2", ""),
        "availability": payload.get("availability", ""),
        "custom_note": payload.get("custom_note", ""),
        "disclaimer": payload.get("disclaimer", ""),
    }
    out = payload.get("custom_html") or ""
    for key, val in values.items():
        out = out.replace("{{" + key + "}}", str(escape(val)))
    out = out.replace("{{logo}}", logo_tag)
    return out


def render_signature_html(payload):
    if payload.get("template") == CUSTOM_TEMPLATE:
        return render_custom_html(payload)
    template_file = SIGNATURE_TEMPLATES.get(
        payload.get("template"), SIGNATURE_TEMPLATES[DEFAULT_SIGNATURE_TEMPLATE]
    )
    return render_template(template_file, **payload)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )


def get_or_create_api_key():
    """Return the current API key, creating one on first call."""
    key = get_setting("api_key")
    if not key:
        key = str(uuid.uuid4())
        set_setting("api_key", key)
    return key


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def generate_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]

app.jinja_env.globals["csrf_token"] = generate_csrf_token


@app.before_request
def check_csrf():
    """CSRF-Schutz für alle POST-Requests ausser API-Endpunkte."""
    if request.method != "POST":
        return
    if request.path.startswith("/api/"):
        return
    token = session.get("csrf_token", "")
    form_token = request.form.get("csrf_token", "")
    if not token or not form_token or not hmac.compare_digest(token, form_token):
        abort(403)


def is_safe_redirect_url(target):
    """Verhindert Open Redirect auf externe Domains."""
    ref = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    return test.scheme in ("http", "https") and ref.netloc == test.netloc


def require_api_key(f):
    """Protect public API endpoints with a shared API key.
    Key must be sent via X-Api-Key request header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = get_or_create_api_key()
        provided = request.headers.get("X-Api-Key", "")
        if not hmac.compare_digest(provided, expected):
            return Response(
                "Unauthorized – API-Key fehlt oder ungültig.\n"
                "Header: X-Api-Key: <key>\n",
                status=401,
                mimetype="text/plain"
            )
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db() as conn:
            # First-time setup: no users yet → create first admin
            count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if count == 0:
                conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,1)",
                    (username, generate_password_hash(password, method="pbkdf2:sha256"))
                )
                user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                session["user_id"]   = user["id"]
                session["username"]  = user["username"]
                session["is_admin"]  = user["is_admin"]
                flash(_("Erster Administrator-Account erstellt."), "success")
                return redirect(url_for("index"))
            user = conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = user["is_admin"]
            # UI-Sprachpräferenz aus dem Konto laden (falls gesetzt)
            try:
                if user["language"] in SUPPORTED_LANGUAGES:
                    session["lang"] = user["language"]
            except (IndexError, KeyError):
                pass
            next_url = request.args.get("next", "")
            if next_url and is_safe_redirect_url(next_url):
                return redirect(next_url)
            return redirect(url_for("index"))
        flash(_("Benutzername oder Passwort falsch."), "danger")
    with get_db() as conn:
        first_time = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0
    return render_template("login.html", first_time=first_time)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/set-language/<lang>")
def set_language(lang):
    """Sprache umschalten. Setzt ein Cookie (gilt auch vor dem Login);
    wenn eingeloggt, wird die Wahl zusätzlich im Konto gespeichert."""
    if lang not in SUPPORTED_LANGUAGES:
        lang = app.config["BABEL_DEFAULT_LOCALE"]
    # Bei eingeloggten Nutzern dauerhaft im Konto speichern
    if session.get("user_id"):
        session["lang"] = lang
        with get_db() as conn:
            conn.execute("UPDATE users SET language=? WHERE id=?",
                         (lang, session["user_id"]))
    # Zurück zur vorherigen Seite (oder Startseite)
    target = request.referrer if request.referrer and is_safe_redirect_url(request.referrer) else url_for("index")
    resp = redirect(target)
    # Cookie ein Jahr gültig
    resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365,
                    httponly=True, samesite="Lax")
    return resp


# ---------------------------------------------------------------------------
# Routes – Benutzerverwaltung
# ---------------------------------------------------------------------------

@app.route("/users")
@login_required
def user_list():
    if not session.get("is_admin"):
        abort(403)
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("users.html", users=users)


@app.route("/users/new", methods=["GET", "POST"])
@login_required
def user_new():
    if not session.get("is_admin"):
        abort(403)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        is_admin = 1 if request.form.get("is_admin") else 0
        if not username or not password:
            flash(_("Benutzername und Passwort sind Pflichtfelder."), "danger")
            return render_template("user_form.html", user=None)
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,?)",
                    (username, generate_password_hash(password, method="pbkdf2:sha256"), is_admin)
                )
            flash(_("Benutzer '%(name)s' erstellt.", name=username), "success")
            return redirect(url_for("user_list"))
        except sqlite3.IntegrityError:
            flash(_("Benutzername bereits vergeben."), "danger")
    return render_template("user_form.html", user=None)


@app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@login_required
def user_edit(uid):
    if not session.get("is_admin") and session.get("user_id") != uid:
        abort(403)
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        abort(404)
    if request.method == "POST":
        password = request.form.get("password", "").strip()
        is_admin = 1 if request.form.get("is_admin") else 0
        with get_db() as conn:
            if password:
                conn.execute(
                    "UPDATE users SET password_hash=?, is_admin=? WHERE id=?",
                    (generate_password_hash(password, method="pbkdf2:sha256"), is_admin, uid)
                )
            else:
                conn.execute(
                    "UPDATE users SET is_admin=? WHERE id=?",
                    (is_admin, uid)
                )
        flash(_("Benutzer aktualisiert."), "success")
        return redirect(url_for("user_list") if session.get("is_admin") else url_for("index"))
    return render_template("user_form.html", user=user)


@app.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
def user_delete(uid):
    if not session.get("is_admin"):
        abort(403)
    if uid == session.get("user_id"):
        flash(_("Du kannst deinen eigenen Account nicht löschen."), "danger")
        return redirect(url_for("user_list"))
    with get_db() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if remaining <= 1:
            flash(_("Der letzte Benutzer kann nicht gelöscht werden."), "danger")
            return redirect(url_for("user_list"))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
    flash(_("Benutzer gelöscht."), "success")
    return redirect(url_for("user_list"))


# ---------------------------------------------------------------------------
# Routes – Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    with get_db() as conn:
        organizations = conn.execute("SELECT COUNT(*) AS c FROM organizations").fetchone()["c"]
        employees = conn.execute("SELECT COUNT(*) AS c FROM employees WHERE active=1").fetchone()["c"]
        inactive  = conn.execute("SELECT COUNT(*) AS c FROM employees WHERE active=0").fetchone()["c"]
    return render_template("index.html",
                           organization_count=organizations,
                           employee_count=employees,
                           inactive_count=inactive)


# ---------------------------------------------------------------------------
# Routes – Organizations
# ---------------------------------------------------------------------------

@app.route("/organizations")
@login_required
def organizations():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.*, COUNT(e.id) AS emp_count
            FROM organizations p
            LEFT JOIN employees e ON e.organization_id = p.id AND e.active = 1
            GROUP BY p.id ORDER BY p.name
        """).fetchall()
    return render_template("organizations.html", organizations=rows)


@app.route("/organizations/new", methods=["GET", "POST"])
@login_required
def organization_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash(_("Name der Organisation ist erforderlich."), "danger")
            return render_template("organization_form.html", organization=None,
                                   default_custom_note=DEFAULT_CUSTOM_NOTE,
                                   default_disclaimer=DEFAULT_DISCLAIMER)
        logo = None
        file = request.files.get("logo")
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_name = f"{uuid.uuid4().hex}_{filename}"
            file.save(LOGOS_DIR / unique_name)
            logo = unique_name

        with get_db() as conn:
            conn.execute("""
                INSERT INTO organizations (name, address1, address2, phone, logo, custom_note, disclaimer, template, accent_color, custom_html)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name,
                  request.form.get("address1", "").strip(),
                  request.form.get("address2", "").strip(),
                  request.form.get("phone", "").strip(),
                  logo,
                  request.form.get("custom_note", "").strip() or DEFAULT_CUSTOM_NOTE,
                  request.form.get("disclaimer", "").strip() or DEFAULT_DISCLAIMER,
                  normalize_template(request.form.get("template", DEFAULT_SIGNATURE_TEMPLATE)),
                  normalize_color(request.form.get("accent_color")),
                  request.form.get("custom_html", "").strip()))
        flash(_("Organisation «%(name)s» erstellt.", name=name), "success")
        return redirect(url_for("organizations"))
    return render_template("organization_form.html", organization=None,
                           default_custom_note=DEFAULT_CUSTOM_NOTE,
                           default_disclaimer=DEFAULT_DISCLAIMER)


@app.route("/organizations/<int:oid>/edit", methods=["GET", "POST"])
@login_required
def organization_edit(oid):
    with get_db() as conn:
        organization = conn.execute("SELECT * FROM organizations WHERE id=?", (oid,)).fetchone()
        if not organization:
            abort(404)
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash(_("Name der Organisation ist erforderlich."), "danger")
                return render_template("organization_form.html", organization=organization,
                                       default_custom_note=DEFAULT_CUSTOM_NOTE,
                                       default_disclaimer=DEFAULT_DISCLAIMER)
            logo = organization["logo"]
            file = request.files.get("logo")
            if file and file.filename and allowed_file(file.filename):
                if logo:
                    old = LOGOS_DIR / logo
                    if old.exists():
                        old.unlink()
                filename = secure_filename(file.filename)
                unique_name = f"{uuid.uuid4().hex}_{filename}"
                file.save(LOGOS_DIR / unique_name)
                logo = unique_name

            conn.execute("""
                UPDATE organizations SET name=?, address1=?, address2=?, phone=?,
                logo=?, custom_note=?, disclaimer=?, template=?, accent_color=?, custom_html=?, updated_at=datetime('now')
                WHERE id=?
            """, (name,
                  request.form.get("address1", "").strip(),
                  request.form.get("address2", "").strip(),
                  request.form.get("phone", "").strip(),
                  logo,
                  request.form.get("custom_note", "").strip() or DEFAULT_CUSTOM_NOTE,
                  request.form.get("disclaimer", "").strip() or DEFAULT_DISCLAIMER,
                  normalize_template(request.form.get("template", DEFAULT_SIGNATURE_TEMPLATE)),
                  normalize_color(request.form.get("accent_color")),
                  request.form.get("custom_html", "").strip(),
                  oid))
            flash(_("Organisation «%(name)s» gespeichert.", name=name), "success")
            return redirect(url_for("organizations"))
    return render_template("organization_form.html", organization=organization,
                           default_custom_note=DEFAULT_CUSTOM_NOTE,
                           default_disclaimer=DEFAULT_DISCLAIMER)


@app.route("/organizations/<int:oid>/delete", methods=["POST"])
@login_required
def organization_delete(oid):
    with get_db() as conn:
        row = conn.execute("SELECT name, logo FROM organizations WHERE id=?", (oid,)).fetchone()
        if not row:
            abort(404)
        if row["logo"]:
            f = LOGOS_DIR / row["logo"]
            if f.exists():
                f.unlink()
        conn.execute("DELETE FROM organizations WHERE id=?", (oid,))
    flash(_("Organisation «%(name)s» gelöscht.", name=row["name"]), "success")
    return redirect(url_for("organizations"))


# ---------------------------------------------------------------------------
# Routes – Employees
# ---------------------------------------------------------------------------

@app.route("/employees")
@login_required
def employees():
    search = request.args.get("q", "").strip()
    oid = request.args.get("organization", "")
    sort = request.args.get("sort", "name")
    direction = request.args.get("dir", "asc").lower()
    if direction not in ("asc", "desc"):
        direction = "asc"

    sort_map = {
        "name":     "COALESCE(NULLIF(e.last_name, ''), e.first_name) COLLATE NOCASE, e.first_name COLLATE NOCASE",
        "first":    "e.first_name COLLATE NOCASE",
        "email":    "e.email COLLATE NOCASE",
        "phone":    "e.phone COLLATE NOCASE",
        "organization": "p.name COLLATE NOCASE, e.last_name COLLATE NOCASE",
        "active":   "e.active",
    }
    order_expr = sort_map.get(sort, sort_map["name"])

    with get_db() as conn:
        query = """
            SELECT e.*, p.name AS organization_name
            FROM employees e JOIN organizations p ON p.id = e.organization_id
        """
        params = []
        where = []
        if search:
            where.append("(e.first_name LIKE ? OR e.last_name LIKE ? OR e.email LIKE ?)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]
        if oid:
            where.append("e.organization_id = ?")
            params.append(oid)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += f" ORDER BY {order_expr} {direction.upper()}"
        rows = conn.execute(query, params).fetchall()
        organization_list = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()
    return render_template("employees.html", employees=rows,
                           organizations=organization_list, search=search, oid=oid,
                           sort=sort, direction=direction)


@app.route("/employees/new", methods=["GET", "POST"])
@login_required
def employee_new():
    with get_db() as conn:
        organization_list = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            first = request.form.get("first_name", "").strip()
            last  = request.form.get("last_name", "").strip()
            oid   = request.form.get("organization_id", "")
            sig_label = request.form.get("sig_label", "").strip()
            if not (email and first and oid):
                flash(_("E-Mail, Vorname und Organisation sind Pflichtfelder."), "danger")
                return render_template("employee_form.html", employee=None,
                                       organizations=organization_list)
            new_uuid = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO employees
                    (organization_id, email, title, first_name, last_name, phone, availability, sig_label, sig_uuid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (oid, email,
                  request.form.get("title", "").strip(),
                  first, last or None,
                  request.form.get("phone", "").strip(),
                  request.form.get("availability", "").strip() or None,
                  sig_label or None,
                  new_uuid))
            display = f"{first} {last}".strip()
            flash(_("%(name)s wurde angelegt.", name=display), "success")
            return redirect(url_for("employees"))
    return render_template("employee_form.html", employee=None, organizations=organization_list)


@app.route("/employees/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def employee_edit(eid):
    with get_db() as conn:
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        if not emp:
            abort(404)
        organization_list = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            first = request.form.get("first_name", "").strip()
            last  = request.form.get("last_name", "").strip()
            oid   = request.form.get("organization_id", "")
            sig_label = request.form.get("sig_label", "").strip()
            if not (email and first and oid):
                flash(_("E-Mail, Vorname und Organisation sind Pflichtfelder."), "danger")
                return render_template("employee_form.html", employee=emp,
                                       organizations=organization_list)
            conn.execute("""
                UPDATE employees SET organization_id=?, email=?, title=?, first_name=?,
                last_name=?, phone=?, availability=?, sig_label=?, active=?,
                updated_at=datetime('now') WHERE id=?
            """, (oid, email,
                  request.form.get("title", "").strip(),
                  first, last or None,
                  request.form.get("phone", "").strip(),
                  request.form.get("availability", "").strip() or None,
                  sig_label or None,
                  1 if request.form.get("active") else 0,
                  eid))
            display = f"{first} {last}".strip()
            flash(_("%(name)s gespeichert.", name=display), "success")
            return redirect(url_for("employees"))
    return render_template("employee_form.html", employee=emp, organizations=organization_list)


@app.route("/employees/<int:eid>/delete", methods=["POST"])
@login_required
def employee_delete(eid):
    with get_db() as conn:
        row = conn.execute("SELECT first_name, last_name FROM employees WHERE id=?", (eid,)).fetchone()
        if not row:
            abort(404)
        conn.execute("DELETE FROM employees WHERE id=?", (eid,))
    display = f"{row['first_name']} {row['last_name'] or ''}".strip()
    flash(_("%(name)s gelöscht.", name=display), "success")
    return redirect(url_for("employees"))


# ---------------------------------------------------------------------------
# Routes – CSV Import
# ---------------------------------------------------------------------------

@app.route("/employees/import", methods=["GET", "POST"])
@login_required
def employee_import():
    with get_db() as conn:
        organization_list = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()

    if request.method == "POST":
        file = request.files.get("csv_file")
        oid  = request.form.get("organization_id", "")
        if not oid:
            flash(_("Bitte eine Organisation auswählen."), "danger")
            return render_template("import.html", organizations=organization_list)
        if not file or not file.filename.endswith(".csv"):
            flash(_("Bitte eine CSV-Datei hochladen."), "danger")
            return render_template("import.html", organizations=organization_list)

        stream = io.StringIO(file.read().decode("utf-8-sig"), newline="")
        reader = csv.DictReader(stream, delimiter=request.form.get("delimiter", ","))

        col = {
            "email":        request.form.get("col_email", "email"),
            "title":        request.form.get("col_title", "title"),
            "first_name":   request.form.get("col_first", "first_name"),
            "last_name":    request.form.get("col_last", "last_name"),
            "phone":        request.form.get("col_phone", "phone"),
            "availability": request.form.get("col_availability", "availability"),
            "sig_label":    request.form.get("col_sig_label", "sig_label"),
        }

        added = updated = skipped = 0
        errors = []
        with get_db() as conn:
            for i, row in enumerate(reader, start=2):
                email = row.get(col["email"], "").strip().lower()
                first = row.get(col["first_name"], "").strip()
                last  = row.get(col["last_name"], "").strip()
                availability = row.get(col["availability"], "").strip()
                sig_label    = row.get(col["sig_label"], "").strip()
                if not (email and first):
                    errors.append(f"Zeile {i}: E-Mail oder Vorname fehlt – übersprungen.")
                    skipped += 1
                    continue
                existing = conn.execute(
                    "SELECT id FROM employees WHERE email=? AND (sig_label=? OR (sig_label IS NULL AND ?=''))",
                    (email, sig_label, sig_label)
                ).fetchone()
                if existing:
                    conn.execute("""
                        UPDATE employees SET title=?, first_name=?, last_name=?, phone=?,
                        availability=?, sig_label=?, organization_id=?, updated_at=datetime('now')
                        WHERE id=?
                    """, (row.get(col["title"], "").strip(),
                          first, last or None,
                          row.get(col["phone"], "").strip(),
                          availability or None,
                          sig_label or None,
                          oid, existing["id"]))
                    updated += 1
                else:
                    conn.execute("""
                        INSERT INTO employees
                            (organization_id, email, title, first_name, last_name, phone, availability, sig_label, sig_uuid)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (oid, email,
                          row.get(col["title"], "").strip(),
                          first, last or None,
                          row.get(col["phone"], "").strip(),
                          availability or None,
                          sig_label or None,
                          str(uuid.uuid4())))
                    added += 1

        flash(_("Import abgeschlossen: %(a)s neu, %(u)s aktualisiert, %(s)s übersprungen.", a=added, u=updated, s=skipped), "success")
        for e in errors[:10]:
            flash(e, "warning")
        return redirect(url_for("employees"))

    return render_template("import.html", organizations=organization_list)


# ---------------------------------------------------------------------------
# Routes – Preview
# ---------------------------------------------------------------------------

@app.route("/employees/<int:eid>/preview")
@login_required
def employee_preview(eid):
    with get_db() as conn:
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
        if not emp:
            abort(404)
        organization = conn.execute("SELECT * FROM organizations WHERE id=?", (emp["organization_id"],)).fetchone()
    payload = build_signature_payload(emp, organization)
    html = render_signature_html(payload)
    return render_template("preview.html", emp=emp, organization=organization,
                           signature_html=html, sig_uuid=payload["sig_uuid"])


# ---------------------------------------------------------------------------
# Routes – Deploy
# ---------------------------------------------------------------------------

@app.route("/deploy/pppc.mobileconfig")
@login_required
def deploy_pppc():
    profile_uuid   = str(uuid.uuid4()).upper()
    payload_uuid   = str(uuid.uuid4()).upper()
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>PayloadDescription</key>
            <string>Erlaubt dem Sig-Manager-Script (osascript) Apple Mail per AppleScript zu steuern.</string>
            <key>PayloadDisplayName</key>
            <string>Sig Manager – Mail Automation</string>
            <key>PayloadIdentifier</key>
            <string>com.mailsign.pppc.mail-automation.{payload_uuid}</string>
            <key>PayloadType</key>
            <string>com.apple.TCC.configuration-profile-policy</string>
            <key>PayloadUUID</key>
            <string>{payload_uuid}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
            <key>Services</key>
            <dict>
                <key>AppleEvents</key>
                <array>
                    <dict>
                        <key>Allowed</key>
                        <true/>
                        <key>CodeRequirement</key>
                        <string>identifier &quot;com.apple.osascript&quot; and anchor apple</string>
                        <key>Comment</key>
                        <string>osascript – Apple-signiert, wird von deploy_signatures.sh aufgerufen</string>
                        <key>Identifier</key>
                        <string>com.apple.osascript</string>
                        <key>IdentifierType</key>
                        <string>bundleID</string>
                        <key>AEReceiverIdentifier</key>
                        <string>com.apple.mail</string>
                        <key>AEReceiverIdentifierType</key>
                        <string>bundleID</string>
                        <key>AEReceiverCodeRequirement</key>
                        <string>identifier &quot;com.apple.mail&quot; and anchor apple</string>
                    </dict>
                    <dict>
                        <key>Allowed</key>
                        <true/>
                        <key>CodeRequirement</key>
                        <string>anchor apple generic and identifier &quot;com.jamf.management.Jamf&quot; and (certificate leaf[field.1.2.840.113635.100.6.1.9] /* exists */ or certificate 1[field.1.2.840.113635.100.6.2.6] /* exists */ and certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and certificate leaf[subject.OU] = &quot;483DWKW443&quot;)</string>
                        <key>Comment</key>
                        <string>Jamf Management Service (verantwortlicher Prozess laut TCC-Log) – steuert Apple Mail per AppleScript</string>
                        <key>Identifier</key>
                        <string>com.jamf.management.Jamf</string>
                        <key>IdentifierType</key>
                        <string>bundleID</string>
                        <key>AEReceiverIdentifier</key>
                        <string>com.apple.mail</string>
                        <key>AEReceiverIdentifierType</key>
                        <string>bundleID</string>
                        <key>AEReceiverCodeRequirement</key>
                        <string>identifier &quot;com.apple.mail&quot; and anchor apple</string>
                    </dict>
                </array>
            </dict>
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>Erlaubt dem Sig Manager Deployment Script Apple Mail per AppleScript zu steuern.</string>
    <key>PayloadDisplayName</key>
    <string>Sig Manager – Mail Automation PPPC</string>
    <key>PayloadIdentifier</key>
    <string>com.mailsign.pppc.mail-automation</string>
    <key>PayloadScope</key>
    <string>System</string>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>{profile_uuid}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>"""
    return Response(content, mimetype="application/x-apple-aspen-config",
                    headers={"Content-Disposition":
                             "attachment; filename=SigManager-MailAutomation-PPPC.mobileconfig"})


@app.route("/deploy")
@login_required
def deploy():
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM employees WHERE active=1").fetchone()["c"]
    server_url = request.host_url.rstrip("/")
    api_key = get_or_create_api_key()
    return render_template("deploy.html", employee_count=count, server_url=server_url,
                           api_key=api_key)


@app.route("/deploy/regenerate-key", methods=["POST"])
@login_required
def regenerate_api_key():
    new_key = str(uuid.uuid4())
    set_setting("api_key", new_key)
    flash(_("API-Key wurde neu generiert. Bitte das Jamf-Script erneut herunterladen und in Jamf aktualisieren."), "warning")
    return redirect(url_for("deploy"))


@app.route("/deploy/script")
@login_required
def deploy_script():
    server_url = request.host_url.rstrip("/")
    is_localhost = request.host.startswith("localhost") or request.host.startswith("127.")
    if is_localhost:
        flash(_("Achtung: Das Script enthält 'localhost' als Server-URL. "
              "Öffnen Sie das Web-Tool über die echte IP-Adresse des Servers "
              "(z.B. http://192.168.1.x:5050) und laden Sie das Script dann erneut herunter."), "warning")
        return redirect(url_for("deploy"))
    api_key = get_or_create_api_key()
    script = render_template("jamf_script.sh", server_url=server_url, api_key=api_key)
    return Response(script, mimetype="text/x-shellscript",
                    headers={"Content-Disposition": "attachment; filename=deploy_signatures.sh"})


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/signatures.json")
@require_api_key
def api_signatures():
    with get_db() as conn:
        employees = conn.execute("""
            SELECT e.*, p.name AS organization_name, p.address1, p.address2,
                   p.phone AS organization_phone, p.logo, p.custom_note, p.disclaimer
            FROM employees e JOIN organizations p ON p.id = e.organization_id
            WHERE e.active = 1
            ORDER BY e.email COLLATE NOCASE, e.id
        """).fetchall()

    result = []
    for emp in employees:
        organization = {
            "name": emp["organization_name"],
            "address1": emp["address1"],
            "address2": emp["address2"],
            "phone": emp["organization_phone"],
            "logo": emp["logo"],
            "custom_note": emp["custom_note"],
            "disclaimer": emp["disclaimer"],
        }
        payload = build_signature_payload(emp, organization)
        payload["html"] = render_signature_html(payload)
        del payload["logo_b64"]  # already embedded in html
        result.append(payload)

    return jsonify(result)


@app.route("/api/signature/<path:email>")
@require_api_key
def api_signature(email):
    email = email.lower().strip()
    with get_db() as conn:
        emp = conn.execute(
            "SELECT * FROM employees WHERE email=? AND active=1", (email,)
        ).fetchone()
        if not emp:
            abort(404)
        organization = conn.execute(
            "SELECT * FROM organizations WHERE id=?", (emp["organization_id"],)
        ).fetchone()
    payload = build_signature_payload(emp, organization)
    payload["html"] = render_signature_html(payload)
    del payload["logo_b64"]
    return jsonify(payload)


@app.route("/api/signature-html/<path:email>")
@require_api_key
def api_signature_html(email):
    """Raw HTML für AppleScript-basierten Signature-Import."""
    email = email.lower().strip()
    with get_db() as conn:
        emp = conn.execute(
            "SELECT * FROM employees WHERE email=? AND active=1", (email,)
        ).fetchone()
        if not emp:
            abort(404)
        organization = conn.execute(
            "SELECT * FROM organizations WHERE id=?", (emp["organization_id"],)
        ).fetchone()
    payload = build_signature_payload(emp, organization)
    # Logo als URL statt base64 für AppleScript-Deployment:
    # base64 erzeugt ~15KB lange Zeilen, die Mail's content-Property leer lässt.
    if organization["logo"]:
        base_url = request.host_url.rstrip("/")
        payload["logo_b64"] = f"{base_url}/static/logos/{organization['logo']}"
    html = render_signature_html(payload)
    name_b64 = base64.b64encode(payload["sig_name"].encode("utf-8")).decode("ascii")
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["X-Sig-Name-B64"] = name_b64
    resp.headers["X-Sig-UUID"]     = payload["sig_uuid"]
    return resp


@app.route("/api/mailsignatures-list/<path:email>")
@require_api_key
def api_mailsignatures_list(email):
    """Return tab-separated 'uuid\\tbase64name' per line – Jamf script needs no Python to parse."""
    email = email.lower().strip()
    with get_db() as conn:
        emps = conn.execute(
            "SELECT e.*, p.name AS organization_name, p.address1, p.address2, "
            "p.phone AS organization_phone, p.logo, p.custom_note, p.disclaimer "
            "FROM employees e JOIN organizations p ON p.id = e.organization_id "
            "WHERE LOWER(e.email)=? AND e.active=1 ORDER BY e.id ASC",
            (email,)
        ).fetchall()
    if not emps:
        abort(404)
    lines = []
    for emp in emps:
        organization = {
            "name": emp["organization_name"], "address1": emp["address1"],
            "address2": emp["address2"],  "phone": emp["organization_phone"],
            "logo": emp["logo"],          "custom_note": emp["custom_note"],
            "disclaimer": emp["disclaimer"],
        }
        payload = build_signature_payload(emp, organization)
        name_b64 = base64.b64encode(payload["sig_name"].encode("utf-8")).decode("ascii")
        lines.append(f"{payload['sig_uuid']}\t{name_b64}")
    return Response("\n".join(lines) + "\n", mimetype="text/plain; charset=utf-8")


@app.route("/api/mailsignature-by-uuid/<sig_uuid>")
@require_api_key
def api_mailsignature_by_uuid(sig_uuid):
    """Return a ready-to-write .mailsignature file for a specific UUID."""
    with get_db() as conn:
        emp = conn.execute(
            "SELECT * FROM employees WHERE sig_uuid=? AND active=1", (sig_uuid,)
        ).fetchone()
        if not emp:
            abort(404)
        organization = conn.execute(
            "SELECT * FROM organizations WHERE id=?", (emp["organization_id"],)
        ).fetchone()

    payload = build_signature_payload(emp, organization)
    html = render_signature_html(payload)

    qp = quopri.encodestring(html.encode("utf-8"), header=False).decode("ascii")
    content = (
        "Content-Transfer-Encoding: quoted-printable\n"
        "Content-Type: text/html;\n\tcharset=utf-8\n"
        f"Message-Id: <{payload['sig_uuid']}@local>\n"
        "Mime-Version: 1.0 (Mac OS X Mail)\n\n"
        + qp
    )
    name_b64 = base64.b64encode(payload["sig_name"].encode("utf-8")).decode("ascii")
    resp = Response(content, mimetype="text/plain; charset=utf-8")
    resp.headers["X-Sig-UUID"]     = payload["sig_uuid"]
    resp.headers["X-Sig-Name-B64"] = name_b64
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{payload["sig_uuid"]}.mailsignature"'
    )
    return resp


@app.route("/api/mailsignature/<path:email>")
@require_api_key
def api_mailsignature(email):
    """Return the PRIMARY (first) .mailsignature for an email – backward compat."""
    email = email.lower().strip()
    with get_db() as conn:
        emp = conn.execute(
            "SELECT * FROM employees WHERE LOWER(email)=? AND active=1 ORDER BY id ASC LIMIT 1",
            (email,)
        ).fetchone()
        if not emp:
            abort(404)
        organization = conn.execute(
            "SELECT * FROM organizations WHERE id=?", (emp["organization_id"],)
        ).fetchone()

    payload = build_signature_payload(emp, organization)
    html = render_signature_html(payload)

    qp = quopri.encodestring(html.encode("utf-8"), header=False).decode("ascii")
    content = (
        "Content-Transfer-Encoding: quoted-printable\n"
        "Content-Type: text/html;\n\tcharset=utf-8\n"
        f"Message-Id: <{payload['sig_uuid']}@local>\n"
        "Mime-Version: 1.0 (Mac OS X Mail)\n\n"
        + qp
    )

    # Encode name as base64 so non-ASCII chars survive HTTP headers safely
    name_b64 = base64.b64encode(payload["sig_name"].encode("utf-8")).decode("ascii")

    resp = Response(content, mimetype="text/plain; charset=utf-8")
    resp.headers["X-Sig-UUID"]     = payload["sig_uuid"]
    resp.headers["X-Sig-Name-B64"] = name_b64
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{payload["sig_uuid"]}.mailsignature"'
    )
    return resp


@app.route("/api/csv-template")
@login_required
def csv_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "title", "first_name", "last_name", "phone", "availability", "sig_label"])
    writer.writerow([
        "max.muster@beispiel.ch", "", "Max", "Muster",
        "+41 44 000 00 00", "Mo-Fr 08:00 - 17:00 Uhr", "",
    ])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=mitarbeiter_vorlage.csv"})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
# DB beim Import initialisieren/migrieren, damit es auch unter gunicorn
# (das app:app importiert und den __main__-Block NICHT ausführt) passiert.
init_db()
migrate_db()


# ---------------------------------------------------------------------------
# Main (lokaler Entwicklungsserver)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.jinja_env.auto_reload = True
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(host="0.0.0.0", port=port, debug=False)
