# Imagen oficial de Playwright para Python: ya trae Chromium y sus dependencias.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Instala FastAPI y uvicorn
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el código
COPY . .

# Arranca la API. Render pone el puerto en la variable $PORT.
RUN chmod +x start.sh
COPY . .

RUN chmod +x start.sh
CMD ["bash", "start.sh"]
