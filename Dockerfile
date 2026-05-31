FROM python:3.12-slim

WORKDIR /app

# Abhängigkeiten zuerst (Layer-Cache nutzen)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code kopieren
COPY . .

# Übersetzungen kompilieren (.po -> .mo), falls vorhanden
RUN if [ -d translations ]; then pybabel compile -d translations || true; fi

# Persistente Verzeichnisse sicherstellen
RUN mkdir -p data static/logos

EXPOSE 5050

# Produktion: gunicorn. Beim Start wird die DB initialisiert/migriert (siehe app.py Bootstrap).
CMD ["gunicorn", "-b", "0.0.0.0:5050", "-w", "2", "app:app"]
