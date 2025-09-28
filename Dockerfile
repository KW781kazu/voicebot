# ===== Base =====
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# （必要に応じてビルドツール。不要なら削ってOK）
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# 依存を先に入れてレイヤーキャッシュを効かせる
COPY requirements.txt .
RUN pip install -r requirements.txt

# ★ アプリ本体をコピー（status_routes.py を忘れずに）
COPY app.py status_routes.py . 

# その他のファイルがあれば必要に応じて追加
# 例) COPY DEPLOY_STAMP.txt . 
# 例) COPY . .  ← リポジトリ全体を入れたい場合はこちらでもOK

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
