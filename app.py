# app.py 〈全文〉
# 機能:
# - /health, /version
# - /twiml（固定テスト応答）
# - /twiml_stream（ダミー発声→挨拶→<Connect><Stream>）
# - /stream（Twilio Media Streams 受信 + VAD=発話検知のログ）
# 起動例: uvicorn app:app --host 0.0.0.0 --port 8080

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os, json, traceback, base64, audioop

APP_NAME = "voicebot"
APP_VERSION = "0.4.0"  # VADログ対応

# ---- Audio/VAD 設定 ----
SAMPLE_RATE = 8000          # Twilio Media Streams は PCMU 8kHz
FRAME_MS = 20               # 1フレーム=20ms
RMS_THRESHOLD = 450         # 発話判定のしきい値（環境で調整）
HANGOVER_FRAMES = 8         # 終了判定の猶予（無音が続いたらEND）

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# ========== 基本ルート ==========
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

# ========== 固定TwiML（動作確認用） ==========
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

# ========== 挨拶 → <Connect><Stream> ==========
# TwiML App / 電話番号の Webhook は https://voice.frontglass.net/twiml_stream
@app.get("/twiml_stream")
@app.post("/twiml_stream")
async def twiml_stream():
    ws_url = "wss://voice.frontglass.net/stream"
    # ダミー発声でTTSの立ち上がりを安定化
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Say language="ja-JP">テスト</Say>'
        '<Say language="ja-JP">接続テストを開始します。</Say>'
        f'<Connect><Stream url="{ws_url}"/></Connect>'
        '</Response>'
    )
    return Response(content=xml, media_type="text/xml")

# ========== WebSocket（Twilio Media Streams）==========
# 受信イベント:
# - "start" / "connected" / "media" / "mark" / "stop" など
# "media" の payload は base64 PCMU(μ-law) 20ms フレーム
@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    await ws.accept()
    start_ts = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN at {start_ts}", flush=True)

    # VAD 状態
    msg_count = 0
    speaking = False
    hangover = 0

    try:
        while True:
            text = await ws.receive_text()
            msg_count += 1

            # 最初の数件はイベント種別だけ軽く出す
            if msg_count <= 5:
                try:
                    peek = json.loads(text)
                    print(f"[WS] #{msg_count} event={peek.get('event','unknown')}", flush=True)
                except Exception:
                    print(f"[WS] #{msg_count} (non-JSON?)", flush=True)

            # 本処理
            try:
                evt = json.loads(text)
            except Exception:
                continue

            etype = evt.get("event")
            if etype == "media":
                payload_b64 = evt.get("media", {}).get("payload")
                if not payload_b64:
                    continue
                try:
                    ulaw = base64.b64decode(payload_b64)
                    # μ-law → 16bit PCM へ変換（幅=2）
                    lin16 = audioop.ulaw2lin(ulaw, 2)
                    rms = audioop.rms(lin16, 2)  # 0..~3000程度
                except Exception:
                    continue

                # ---- 簡易VAD ----
                if rms >= RMS_THRESHOLD:
                    hangover = HANGOVER_FRAMES
                    if not speaking:
                        speaking = True
                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                        print(f"[VAD] START rms={rms} @{ts}", flush=True)
                else:
                    if speaking:
                        if hangover > 0:
                            hangover -= 1
                        else:
                            speaking = False
                            ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                            print(f"[VAD] END   rms={rms} @{ts}", flush=True)

                # 進捗ログ（控えめ）
                if msg_count % 200 == 0:
                    print(f"[WS] received {msg_count} messages (rms={rms})", flush=True)

            elif etype in ("start", "connected", "mark", "stop"):
                print(f"[WS] {etype}", flush=True)

    except WebSocketDisconnect:
        end_ts = datetime.now(timezone.utc).isoformat()
        print(f"[WS] CLOSE at {end_ts}, total={msg_count}", flush=True)
    except Exception:
        end_ts = datetime.now(timezone.utc).isoformat()
        print(f"[WS] ERROR at {end_ts}, total={msg_count}", flush=True)
        traceback.print_exc()
        try:
            await ws.close()
        except Exception:
            pass
