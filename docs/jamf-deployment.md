# Jamf deployment

How to roll MailSign signatures out to your fleet with Jamf Pro. You do this **once**;
afterwards you only change data in the web app.

## Prerequisites

- MailSign is installed and reachable over HTTPS (see [installation.md](installation.md)).
- At least one organization with members exists, and the previews look right.
- You have admin access to Jamf Pro.

## 1. Download the artifacts

In the web app, open **Deploy**. Download both:

1. **PPPC profile** — `MailSign-MailAutomation-PPPC.mobileconfig`
   Grants the deploy script permission to control Apple Mail via AppleScript. Without it,
   the script can't find the mail accounts.
2. **Deploy script** — `deploy_signatures.sh`
   Pre-configured with your server URL and API key. No editing needed.

## 2. Upload the PPPC profile (configuration profile)

1. Jamf Pro → **Computers → Configuration Profiles → Upload**.
2. Upload `MailSign-MailAutomation-PPPC.mobileconfig`.
3. Set the **scope** to all target devices → **Save**.

Deploy this profile *before* (or together with) the script so permissions are in place
when the script first runs.

## 3. Add the script

1. Jamf Pro → **Settings → Computer Management → Scripts → New**.
2. Upload / paste `deploy_signatures.sh`.
3. Give it a name (e.g. *MailSign – Deploy signatures*) → **Save**.

## 4. Create the policy

1. **Computers → Policies → New**.
2. **Trigger:** *Login* and/or *Recurring Check-in* (optionally *Once per week*).
3. **Scripts:** add the uploaded script.
4. **Scope:** the desired devices / groups.
5. Enable and **Save**.

The script runs **as the logged-in user** (not root), detects that user's Mail account,
fetches their signature from the server, and writes the `.mailsignature` file. It assigns
the signature via `accountsmap.plist` and is idempotent — re-runs update instead of
duplicating.

## 5. Updating signatures later

When anything changes (new member, new logo, edited text):

1. Change the data in the **web app**.
2. That's it. On its next run the policy fetches the latest data.

**No need to re-upload the script to Jamf** — it always pulls current data from the server.

## Notes & gotchas

- The script uses only **built-in macOS tools** — no Python or Command Line Tools needed on clients.
- **Apple Mail should be closed** during deployment.
- The client must reach the **server URL** shown on the Deploy page (internal network is fine).
- Signature UUIDs are derived from the email address → **stable across updates**.
- Supported: **macOS 12 Monterey – 15 Sequoia**.
- For a manual test without Jamf you can run the script with `-e user@example.com`.

## If signatures don't appear

- Confirm the **PPPC profile** is installed on the device (otherwise AppleScript access to Mail is blocked).
- Confirm the device can reach the server URL.
- Make sure Mail was closed when the policy ran; trigger the policy again.
