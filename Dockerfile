# Базовый образ с Python 3.11
FROM python:3.11-slim

# Системные пакеты (ffmpeg пригодится)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Запуск бота на long-polling
CMD ["python", "app.py"]
