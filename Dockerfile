# Imagen oficial de Playwright para Python: ya trae Chromium y sus dependencias.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Instala FastAPI y uvicorn
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el código
COPY . .

# Arranca la API. Render pone el puerto en la variable $PORT.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
