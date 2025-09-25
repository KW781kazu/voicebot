# app.py 〈全文〉
# FastAPI アプリ本体。/health, /version, /twiml を提供します。
# uvicorn 起動コマンド（参考）：uvicorn app:app --host 0.0.0.0 --port 8080

from fastapi import FastAPI, Response
from datetime import datetime, timezone
import os

# ===== アプリ情報 =====
APP_NAME = "voicebot"
APP_VERSION = "0.1.0"

app = FastAPI(title=APP_NAME, version=APP_VERSION)


# ===== ルート =====
@app.get("/")
async def root_get():
    return {"message": "ok", "app": APP_NAME, "version": APP_VERSION}


# ===== ヘルスチェック =====
@app.get("/health")
async def health_get():
    return {"status": "ok"}


# ===== バージョン情報 =====
# GitHub Actions などで環境変数を注入しておくと便利です:
# - COMMIT_SHA
# - DEPLOY_STAMP (例: ISO8601)
@app.get("/version")
async def version_get():
    commit_sha = os.getenv("COMMIT_SHA", "unknown")
    deploy_stamp_raw = os.getenv("DEPLOY_STAMP", "deployed")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "app": APP_NAME,
        "deploy_stamp": {
            "raw": deploy_stamp_raw,
            "time_utc": now_utc,
            "commit_sha": commit_sha,
        },
    }


# ===== Twilio 用 固定TwiML 応答 =====
# Twilio の Webhook を https://<あなたのドメイン>/twiml に設定します。
# GET/POST のどちらでも応答できるようにしてあります。
@app.get("/twiml")
@app.post("/twiml")
async def twiml():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Say language="ja-JP">こちらはボイスボットのテストです。10秒後に切断します。</Say>'
        '<Pause length="10"/>'
        '<Hangup/>'
        '</Response>'
    )
    return Response(content=xml, media_type="text/xml")
