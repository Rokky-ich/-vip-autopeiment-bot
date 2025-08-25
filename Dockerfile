FROM python:3.11-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y gcc

WORKDIR /app
COPY . .

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

CMD ["python", "bot.py"]
