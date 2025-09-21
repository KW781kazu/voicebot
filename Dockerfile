# Dockerfile（requirements.txt に fastapi/uvicorn あり版）
FROM python:3.11-slim

WORKDIR /app

# 依存は一括で requirements.txt から入れる（キャッシュ効かせるために先にコピー）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリ & デプロイスタンプ
COPY app.py ./app.py
COPY DEPLOY_STAMP.txt ./DEPLOY_STAMP.txt

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
