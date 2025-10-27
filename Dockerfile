FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем все нужные файлы
COPY client.py api.py schema.sql webapp.html backend.py ./

# Используем CMD для запуска обоих скриптов
CMD sh -c "python -u backend.py & python -u client.py && wait"