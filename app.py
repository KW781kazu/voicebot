# app.py 〈全文〉
# - /health, /version
# - /twiml（固定テスト応答）
# - /twiml_stream（ダミー発声→挨拶→<Connect><Stream>）
# - /stream（Twilio Media Streams 受信 + 自動キャリブレーションVAD + ログ）
# 起動例: uvicorn app:app --host 0.0.0.0 --port 8080

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os, json, traceback, base64, audioop

APP_NAME = "voicebot"
APP_VERSION = "0.4.1"  # VAD: 自動キャリブレーション + RMSログ

# ---- Audio/VAD 設定 ----
SAMPLE_RATE = 8000          # Twilio Media Streams は PCMU 8kHz
FRAME_MS = 20               # 1フレーム=20ms（Twilio既定）
CALIB_FRAMES = 50           # 最初の ~1.0秒 を無音キャリブ用に使う
RMS_MULTIPLIER = float(os.getenv("RMS_MULTIPLIER", "3.0"))  # しきい値=無音平均*係数
RMS_MIN = int(os.getenv("RMS_MIN", "120"))                  # 最低しきい値

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
@app.get("/twiml_stream")
@app.post("/twiml_stream")
async def twiml_stream():
    ws_url = "wss://voice.frontglass.net/stream"
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
@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    await ws.accept()
    start_ts = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN at {start_ts}", flush=True)

    # --- VAD 状態 ---
    msg_count = 0
    speaking = False
    hangover = 0
    HANGOVER_FRAMES = 8  # 無音が続いたら END

    # キャリブ用
    calib_rms_sum = 0
    calib_rms_cnt = 0
    threshold = None

    try:
        while True:
            text = await ws.receive_text()
            msg_count += 1

            # 先頭のイベント種別を軽く表示
            if msg_count <= 5:
                try:
                    peek = json.loads(text)
                    print(f"[WS] #{msg_count} event={peek.get('event','unknown')}", flush=True)
                except Exception:
                    print(f"[WS] #{msg_count} (non-JSON?)", flush=True)

            # パース
            try:
                evt = json.loads(text)
            except Exception:
                continue

            etype = evt.get("event")
            if etype != "media":
                if etype in ("start", "connected", "mark", "stop"):
                    print(f"[WS] {etype}", flush=True)
                continue

            # media フレーム
            payload_b64 = evt.get("media", {}).get("payload")
            if not payload_b64:
                continue

            # μ-law → 16bit PCM に変換して RMS を計算
            try:
                ulaw = base64.b64decode(payload_b64)
                lin16 = audioop.ulaw2lin(ulaw, 2)
                rms = audioop.rms(lin16, 2)  # 典型: 無音100前後〜発話時数百〜数千
            except Exception:
                continue

            # ---- 自動キャリブレーション ----
            if threshold is None and calib_rms_cnt < CALIB_FRAMES:
                calib_rms_sum += rms
                calib_rms_cnt += 1
                print(f"[RMS] calib#{calib_rms_cnt}: {rms}", flush=True)
                if calib_rms_cnt == CALIB_FRAMES:
                    noise_avg = (calib_rms_sum / max(1, calib_rms_cnt))
                    threshold = max(int(noise_avg * RMS_MULTIPLIER), RMS_MIN)
                    print(f"[VAD] calibrated: noise_avg={int(noise_avg)} threshold={threshold}", flush=True)
                # 初期200フレームはRMSを毎回出して観測
            else:
                if msg_count <= 200:
                    print(f"[RMS] {rms}", flush=True)
                elif msg_count % 200 == 0:
                    print(f"[RMS] {rms}", flush=True)

            # ---- しきい値がまだなら何もしない ----
            if threshold is None:
                continue

            # ---- 簡易VAD ----
            if rms >= threshold:
                hangover = HANGOVER_FRAMES
                if not speaking:
                    speaking = True
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                    print(f"[VAD] START rms={rms} thr={threshold} @{ts}", flush=True)
            else:
                if speaking:
                    if hangover > 0:
                        hangover -= 1
                    else:
                        speaking = False
                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                        print(f"[VAD] END   rms={rms} thr={threshold} @{ts}", flush=True)

            # 進捗ログ（控えめ）
            if msg_count % 200 == 0:
                print(f"[WS] received {msg_count} messages (rms={rms}, thr={threshold})", flush=True)

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
