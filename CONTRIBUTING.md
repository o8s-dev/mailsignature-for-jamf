# Contributing

Thanks for your interest in improving MailSign! Issues and pull requests are welcome.

## Reporting issues

Open a GitHub issue with:

- what you expected vs. what happened,
- steps to reproduce,
- your environment (macOS version, how you run MailSign, browser).

Please **don't** include real personal data, API keys, or your `SECRET_KEY`.

## Local development

```bash
git clone https://github.com/o8s-dev/mailsignature-for-jamf.git
cd mailsignature-for-jamf

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev-only-not-secret      # dev convenience
pybabel compile -d translations            # build .mo files once
python app.py                              # dev server on http://localhost:5050
```

The SQLite database and migrations run automatically on start. The first visit creates
the admin account.

## Translations (i18n)

UI strings use `gettext` (`_()` in `app.py`, `{{ _('…') }}` in templates). German is the
source language; English lives in `translations/en/…`.

After adding or changing UI strings:

```bash
pybabel extract -F babel.cfg -o messages.pot .          # collect strings
pybabel update -i messages.pot -d translations          # merge into catalogs
# edit translations/en/LC_MESSAGES/messages.po (fill msgstr)
pybabel compile -d translations                          # build .mo
```

- Keep the **German source** (`msgid`) and the German catalog in sync.
- Watch for **fuzzy** matches after `update` — review them; a wrong fuzzy match ships
  the wrong text.
- Don't put straight double quotes (`"`) inside a `_("…")` string in templates — it
  breaks Jinja parsing. Rephrase or use typographic quotes.

## Signature templates

Email HTML is finicky — Apple Mail renders **table layouts + inline styles** most
reliably. New layouts live in `templates/signatures/` and are registered in
`SIGNATURE_TEMPLATES` in `app.py`. Never render user-provided HTML through Jinja
(SSTI risk) — the custom-HTML mode uses safe placeholder substitution.

## Pull requests

- Keep changes focused; describe the what and why.
- Match the existing code style.
- If you touch UI strings, update the translation catalogs.
- Test the affected flow (organization → member → preview → deploy) before submitting.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
