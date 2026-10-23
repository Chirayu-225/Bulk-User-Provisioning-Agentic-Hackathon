FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=7860 \
    HEADLESS=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .
RUN mkdir -p /app/data && chmod -R 777 /app/data /ms-playwright

EXPOSE 7860
CMD ["sh", "-c", "uvicorn deploy_app:app --host 0.0.0.0 --port ${PORT}"]
