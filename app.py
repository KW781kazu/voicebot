# app.py 〈全文〉
# 機能:
# - /health, /version
# - /twiml（固定テスト応答）
# - /twiml_stream（ダミー発声→挨拶→<Connect><Stream>）
# - /stream（Twilio Media Streams 受信 + 自動キャリブレーションVAD + 最初の発話で0.5秒ビープ返信）
# 起動例: uvicorn app:app --host 0.0.0.0 --port 8080

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os, json, traceback, base64, audioop, math, time

APP_NAME = "voicebot"
APP_VERSION = "0.5.0"  # 送信方向（サーバ→Twilio）確認: 0.5秒ビープ返信

# ---- Audio / Stream 設定 ----
SAMPLE_RATE = 8000          # Twilio Media Streams は 8kHz
FRAME_MS = 20               # 1フレーム=20ms
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 160 samples
CALIB_FRAMES = 50           # ~1.0秒 を無音キャリブに使用
RMS_MULTIPLIER = float(os.getenv("RMS_MULTIPLIER", "3.0"))
RMS_MIN = int(os.getenv("RMS_MIN", "120"))
HANGOVER_FRAMES = 8         # 無音継続で END

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

# ========== ユーティリティ：ビープ波形を μ-law で20msフレーム列にする ==========
def gen_beep_ulaw_frames(freq_hz=440.0, duration_sec=0.5, volume=0.4):
    """
    440Hzのサイン波を duration_sec だけ生成し、20msごとの μ-law フレーム(base64文字列)のリストを返す。
    """
    total_samples = int(SAMPLE_RATE * duration_sec)
    frames = []
    t = 0.0
    dt = 1.0 / SAMPLE_RATE
    # 16bit PCM（-32768..32767）で生成 → μ-law へ
    pcm = bytearray()
    for n in range(total_samples):
        s = int(32767 * volume * math.sin(2 * math.pi * freq_hz * t))
        pcm += s.to_bytes(2, byteorder="little", signed=True)
        t += dt
    # 20msごとに分割
    for i in range(0, len(pcm), SAMPLES_PER_FRAME * 2):
        chunk = pcm[i:i + SAMPLES_PER_FRAME * 2]
        # 足りない末尾はゼロでパディング
        if len(chunk) < SAMPLES_PER_FRAME * 2:
            chunk += b"\x00" * (SAMPLES_PER_FRAME * 2 - len(chunk))
        ulaw = audioop.lin2ulaw(bytes(chunk), 2)
        frames.append(base64.b64encode(ulaw).decode("ascii"))
    return frames

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

    # キャリブ
    calib_rms_sum = 0
    calib_rms_cnt = 0
    threshold = None

    # 一度だけビープを返すフラグ
    beep_sent = False

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
                rms = audioop.rms(lin16, 2)
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
                continue  # 閾値が決まるまではVADしない

            if threshold is None:
                continue

            # ---- 簡易VAD ----
            if rms >= threshold:
                hangover = HANGOVER_FRAMES
                if not speaking:
                    speaking = True
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                    print(f"[VAD] START rms={rms} thr={threshold} @{ts}", flush=True)

                    # ★ 初回の発話で0.5秒ビープを一度だけ返す（双方向確認）
                    if not beep_sent:
                        try:
                            frames = gen_beep_ulaw_frames(freq_hz=440.0, duration_sec=0.5, volume=0.4)
                            for b64 in frames:
                                await ws.send_text(json.dumps({"event": "media", "media": {"payload": b64}}))
                                # Twilioは20msフレーム想定：少し待って送出をエミュレート
                                await asyncio_sleep_ms(FRAME_MS)
                            # マークを送っておく（任意）
                            await ws.send_text(json.dumps({"event": "mark", "mark": {"name": "beep_end"}}))
                            beep_sent = True
                            print("[TX] sent 0.5s beep to caller", flush=True)
                        except Exception:
                            traceback.print_exc()

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

# ---- 非同期スリープ（ms）ユーティリティ ----
import asyncio
async def asyncio_sleep_ms(ms: int):
    await asyncio.sleep(ms / 1000.0)
