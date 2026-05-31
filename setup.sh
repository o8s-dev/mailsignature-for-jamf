#!/bin/bash
# =============================================================================
# Sig Manager – Setup-Script für macOS (Mac mini / Server)
# Installiert den Dienst als launchd-Daemon (startet automatisch beim Boot)
# =============================================================================
set -euo pipefail

# ---- Farben -----------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
header()  { echo -e "\n${BOLD}$*${NC}"; }

# ---- Root-Check -------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    error "Bitte als root ausführen:  sudo bash setup.sh"
fi

header "========================================="
header "  Sig Manager – Server Setup"
header "========================================="
echo ""

# ---- Installationspfad bestimmen --------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Installationsverzeichnis: $SCRIPT_DIR"

# ---- Python prüfen ----------------------------------------------------------
PYTHON=$(command -v python3 2>/dev/null || true)
if [ -z "$PYTHON" ]; then
    error "Python 3 nicht gefunden. Bitte installieren: brew install python3"
fi
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PY_VERSION gefunden: $PYTHON"
if python3 -c "import sys; assert sys.version_info >= (3,9)" 2>/dev/null; then
    ok "Python-Version OK (≥ 3.9)"
else
    error "Python ≥ 3.9 benötigt (gefunden: $PY_VERSION)"
fi

# ---- Port konfigurieren -----------------------------------------------------
echo ""
read -r -p "$(echo -e "${BOLD}Port für den Web-Server [5050]:${NC} ")" INPUT_PORT
PORT="${INPUT_PORT:-5050}"

# ---- Service-User konfigurieren --------------------------------------------
echo ""
info "Als welcher Benutzer soll der Dienst laufen?"
info "(Empfohlen: aktueller Admin-User, z.B. $(stat -f %Su /dev/console 2>/dev/null || echo 'admin'))"
CONSOLE_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")
read -r -p "$(echo -e "${BOLD}Benutzername [${CONSOLE_USER:-admin}]:${NC} ")" INPUT_USER
SERVICE_USER="${INPUT_USER:-${CONSOLE_USER:-admin}}"

# Prüfen ob User existiert
if ! id "$SERVICE_USER" &>/dev/null; then
    error "Benutzer '$SERVICE_USER' nicht gefunden."
fi
SERVICE_HOME=$(dscl . -read /Users/"$SERVICE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
ok "Service-User: $SERVICE_USER  (Home: $SERVICE_HOME)"

# ---- Secret Key generieren --------------------------------------------------
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# ---- Virtualenv erstellen ---------------------------------------------------
header "1/5  Abhängigkeiten installieren"

VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtualenv erstellt: $VENV_DIR"
else
    ok "Virtualenv bereits vorhanden"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
ok "Flask und Abhängigkeiten installiert"

# ---- Datenbank initialisieren ----------------------------------------------
header "2/5  Datenbank initialisieren"

chown -R "$SERVICE_USER" "$SCRIPT_DIR"
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python3" -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
from app import init_db; init_db()
print('DB OK')
"
ok "Datenbank initialisiert: $SCRIPT_DIR/data/signatures.db"

# ---- Environment-Datei erstellen -------------------------------------------
header "3/5  Konfiguration speichern"

ENV_FILE="$SCRIPT_DIR/.env"
cat > "$ENV_FILE" << EOF
SECRET_KEY=$SECRET_KEY
PORT=$PORT
EOF
chown "$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "Konfiguration gespeichert: $ENV_FILE"

# ---- Wrapper-Script erstellen -----------------------------------------------
WRAPPER="$SCRIPT_DIR/run_server.sh"
cat > "$WRAPPER" << WRAPPER_EOF
#!/bin/bash
set -a
source "$(dirname "\$0")/.env"
set +a
exec "$(dirname "\$0")/.venv/bin/python3" "$(dirname "\$0")/app.py"
WRAPPER_EOF
chmod +x "$WRAPPER"
chown "$SERVICE_USER" "$WRAPPER"
ok "Wrapper-Script: $WRAPPER"

# ---- LaunchDaemon-Plist installieren ----------------------------------------
header "4/5  launchd-Daemon installieren"

PLIST_NAME="com.mailsign.sig-manager"
PLIST_PATH="/Library/LaunchDaemons/${PLIST_NAME}.plist"
LOG_DIR="/var/log/sig-manager"

mkdir -p "$LOG_DIR"
chown "$SERVICE_USER" "$LOG_DIR"

cat > "$PLIST_PATH" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>UserName</key>
    <string>${SERVICE_USER}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${WRAPPER}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>SECRET_KEY</key>
        <string>${SECRET_KEY}</string>
        <key>PORT</key>
        <string>${PORT}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>

    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLIST_EOF

chmod 644 "$PLIST_PATH"
ok "LaunchDaemon-Plist: $PLIST_PATH"

# ---- Dienst starten ---------------------------------------------------------
header "5/5  Dienst starten"

# Alten Daemon stoppen falls vorhanden
if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
    warn "Alter Dienst läuft — wird neu gestartet…"
    launchctl bootout system "$PLIST_PATH" 2>/dev/null || true
    sleep 1
fi

launchctl bootstrap system "$PLIST_PATH"
sleep 2

# Prüfen ob der Dienst läuft
if launchctl list | grep -q "$PLIST_NAME"; then
    ok "Dienst läuft!"
else
    warn "Dienst nicht in launchctl list — prüfen Sie die Logs:"
    warn "  tail -f $LOG_DIR/stderr.log"
fi

# ---- Firewall-Hinweis -------------------------------------------------------
echo ""
header "========================================="
header "  Setup abgeschlossen"
header "========================================="
echo ""

# Lokale IP ermitteln
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "IP-ADRESSE")

echo -e "  ${GREEN}Web-UI:${NC}        http://${LOCAL_IP}:${PORT}"
echo -e "  ${GREEN}API-Endpunkt:${NC}  http://${LOCAL_IP}:${PORT}/api/signatures.json"
echo ""
echo -e "  ${BOLD}Diese URL im Sig Manager unter 'Jamf Script' eintragen:${NC}"
echo -e "  http://${LOCAL_IP}:${PORT}"
echo ""
echo -e "  ${YELLOW}Logs:${NC}  tail -f ${LOG_DIR}/stderr.log"
echo ""

# Firewall-Status prüfen
FW_STATUS=$(/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null || echo "")
if echo "$FW_STATUS" | grep -q "enabled"; then
    warn "macOS-Firewall ist aktiv. Port $PORT freigeben:"
    echo "     sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $VENV_DIR/bin/python3"
    echo "     sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp $VENV_DIR/bin/python3"
fi

# Jamf Script Hinweis
echo ""
echo -e "  ${BOLD}Nächste Schritte:${NC}"
echo "  1. Web-UI öffnen und Praxen + Mitarbeiter erfassen"
echo "  2. Jamf Script herunterladen (Web-UI → Jamf Script)"
echo "  3. Script in Jamf Pro als Policy hochladen (Trigger: Login)"
echo ""

# ---- Nützliche Befehle ------------------------------------------------------
echo -e "  ${BOLD}Dienst verwalten:${NC}"
echo "  Stoppen:    sudo launchctl bootout system $PLIST_PATH"
echo "  Starten:    sudo launchctl bootstrap system $PLIST_PATH"
echo "  Neu laden:  sudo launchctl kickstart -k system/$PLIST_NAME"
echo "  Logs:       tail -f $LOG_DIR/stderr.log"
echo ""
