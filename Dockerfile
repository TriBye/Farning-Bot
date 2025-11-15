FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system bot && useradd --system --gid bot bot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

USER bot

CMD ["python", "main.py"]
