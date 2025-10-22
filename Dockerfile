FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Amsterdam

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Create uploads directory
RUN mkdir -p /app/uploads && chmod 777 /app/uploads

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY client.py matcher.py api.py webapp.html email_sender.py ./

# Add volume for uploads
VOLUME ["/app/uploads"]

EXPOSE 8080

CMD ["python", "-u", "client.py"]