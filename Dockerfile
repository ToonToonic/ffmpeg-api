FROM python:3.11-slim

# Устанавливаем FFmpeg и системные зависимости
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY . .

# Открываем порт
EXPOSE $PORT

# Запускаем приложение

CMD gunicorn --bind 0.0.0.0:$PORT --timeout 600 --workers 1 app:app


