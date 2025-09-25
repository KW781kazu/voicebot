# app.py 〈全文〉
# /health, /version, /twiml（固定応答）, /twiml_stream（ダミー発声→挨拶→<Connect><Stream>）, /stream（WS受信ログ）
# 起動: uvicorn app:app --host 0.0.0.0 --port 8080

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os
import json
import traceback

APP_NAME = "voicebot"
APP_VERSION = "0.3.2"  # ダミー発声で頭切れ対策

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# ---- 基本 ----
@app.get("/")
async def root_get():
    return {"message": "ok", "app": APP_NAME, "version": APP_VERSION}

@app.get("/health")
async def health_get():
    return {"status": "ok"}

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

# ---- 固定TwiML（確認用に残す）----
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

# ---- ダミー発声 → 挨拶 → <Connect><Stream>（頭切れ対策の本命）----
# TwiML App / 電話番号の Webhook は https://voice.frontglass.net/twiml_stream
@app.get("/twiml_stream")
@app.post("/twiml_stream")
async def twiml_stream():
    ws_url = "wss://voice.frontglass.net/stream"
    # 先頭にごく短い発声「テスト」を挟んで TTS/送出バッファを起こす
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Say language="ja-JP">テスト</Say>'
        '<Say language="ja-JP">接続テストを開始します。</Say>'
        f'<Connect><Stream url="{ws_url}"/></Connect>'
        '</Response>'
    )
    return Response(content=xml, media_type="text/xml")

# ---- WebSocket 受信（Twilio Media Streams）----
@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    await ws.accept()
    msg_count = 0
    start_ts = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN at {start_ts}", flush=True)
    try:
        while True:
            data = await ws.receive_text()  # Twilio はテキストJSON
            msg_count += 1
            if msg_count <= 5:
                try:
                    evt = json.loads(data)
                    event_type = evt.get("event", "unknown")
                    print(f"[WS] #{msg_count} event={event_type}", flush=True)
                except Exception:
                    print(f"[WS] #{msg_count} (non-JSON?)", flush=True)
            if msg_count % 100 == 0:
                print(f"[WS] received {msg_count} messages...", flush=True)
    except WebSocketDisconnect:
        end_ts = datetime.now(timezone.utc).isoformat()
        print(f"[WS] CLOSE at {end_ts}, total={msg_count}", flush=True)
    except Exception:
        end_ts = datetime.now(timezone.utc).isoformat()
        print(f"[WS] ERROR at {end_ts}, total={msg_count}", flush=True)
        traceback.print_exc()
        await ws.close()
