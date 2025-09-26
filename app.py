# app.py 〈全文〉
# 機能:
# - /health, /version
# - /twiml（固定テスト応答）
# - /twiml_stream（ダミー発声→挨拶→<Connect><Stream>）
# - /stream（Twilio Media Streams 受信 → Amazon Transcribe Streaming でリアルタイムSTT）
# 起動例: uvicorn app:app --host 0.0.0.0 --port 8080

from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
import os, json, traceback, base64, audioop, asyncio, uuid

# === Transcribe Streaming SDK ===
# pip install amazon-transcribe
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

APP_NAME = "voicebot"
APP_VERSION = "0.6.0"  # STT(Transcribe) 対応

# ---- Audio / Stream 設定 ----
SAMPLE_RATE = 8000          # Twilio Media Streams は 8kHz
FRAME_MS = 20               # 1フレーム=20ms
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-1")
TRANSCRIBE_LANGUAGE = os.getenv("TRANSCRIBE_LANGUAGE", "ja-JP")  # 例: ja-JP
TRANSCRIBE_VOCAB = os.getenv("TRANSCRIBE_VOCAB", "")             # 任意: カスタム語彙名

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

# ========== Transcribe ハンドラ ==========
class MyTranscriptHandler(TranscriptResultStreamHandler):
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        results = transcript_event.transcript.results
        for res in results:
            if not res.alternatives:
                continue
            text = res.alternatives[0].transcript
            if not text:
                continue
            if res.is_partial:
                print(f"[STT] PARTIAL: {text}", flush=True)
            else:
                print(f"[STT] FINAL  : {text}", flush=True)

# ========== WebSocket（Twilio Media Streams → Transcribe）==========
@app.websocket("/stream")
async def stream_ws(ws: WebSocket):
    await ws.accept()
    call_id = str(uuid.uuid4())[:8]
    start_ts = datetime.now(timezone.utc).isoformat()
    print(f"[WS] OPEN call={call_id} at {start_ts}", flush=True)

    # Transcribe クライアントと双方向ストリームを準備
    client = TranscribeStreamingClient(region=AWS_REGION)
    stream = await client.start_stream_transcription(
        language_code=TRANSCRIBE_LANGUAGE,
        media_sample_rate_hz=SAMPLE_RATE,
        media_encoding="pcm",             # μ-lawから変換後の16bit PCMを送る
        vocabulary_name=TRANSCRIBE_VOCAB or None,
    )

    # 受信→変換→Transcribeへput_audio() するタスク
    async def pump_audio():
        try:
            while True:
                text = await ws.receive_text()
                try:
                    evt = json.loads(text)
                except Exception:
                    continue
                if evt.get("event") != "media":
                    # 進行状況の軽ログ
                    et = evt.get("event")
                    if et in ("connected", "start", "stop", "mark"):
                        print(f"[WS] {et}", flush=True)
                    if et == "stop":
                        break
                    continue

                payload_b64 = evt.get("media", {}).get("payload")
                if not payload_b64:
                    continue
                try:
                    ulaw = base64.b64decode(payload_b64)
                    # μ-law(8kHz) → 16bit PCM へ
                    pcm16 = audioop.ulaw2lin(ulaw, 2)
                except Exception:
                    continue

                await stream.input_stream.send_audio_event(audio_chunk=pcm16)
        except WebSocketDisconnect:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            # 音声入力の終了をTranscribeに通知
            try:
                await stream.input_stream.end_stream()
            except Exception:
                pass

    # Transcribe からのテキスト結果を読むタスク
    async def read_transcripts():
        try:
            handler = MyTranscriptHandler(stream.output_stream)
            await handler.handle_events()
        except Exception:
            traceback.print_exc()

    # 並列実行
    tasks = [
        asyncio.create_task(pump_audio()),
        asyncio.create_task(read_transcripts()),
    ]

    try:
        await asyncio.gather(*tasks)
    except Exception:
        traceback.print_exc()
    finally:
        end_ts = datetime.now(timezone.utc).isoformat()
        print(f"[WS] CLOSE call={call_id} at {end_ts}", flush=True)
        try:
            await ws.close()
        except Exception:
            pass
