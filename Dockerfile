# Для бесплатного деплоя на Hugging Face Spaces (Docker) или Render.
FROM python:3.11-slim

# Hugging Face запускает контейнер от пользователя с UID 1000 — даём ему права на папку data/
RUN useradd -m -u 1000 user
WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .
RUN mkdir -p /app/data && chown -R user /app/data

USER user
ENV HOME=/home/user PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
