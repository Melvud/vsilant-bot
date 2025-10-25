# Dockerfile
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

COPY client.py api.py schema.sql webapp.html ./
# Если у тебя есть бэкенд для webapp.html (например, backend.py), добавь его тоже:
# COPY backend.py ./

# Убедись, что CMD запускает и бота, и твой бэкенд, если он нужен
# Пример для одновременного запуска (может потребоваться process manager):
# CMD sh -c "python backend.py & python client.py"
CMD ["python", "-u", "client.py"] # <-- Оставь это, если бэкенд не нужен или запускается иначе