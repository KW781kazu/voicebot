# app.py
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
import os
from pathlib import Path
from datetime import datetime

app = FastAPI(title="voicebot")

DEPLOY_STAMP_PATH = Path(__file__).parent / "DEPLOY_STAMP.txt"

def _read_deploy_stamp():
    """
    DEPLOY_STAMP.txt の 1行を読み取り:
      例) "redeploy 2025-09-21T03:12:34Z <commitSHA>"
    """
    try:
        txt = DEPLOY_STAMP_PATH.read_text(encoding="utf-8").strip()
        parts = txt.split()
        stamp = {
            "raw": txt,
            "time_utc": None,
            "commit_sha": None,
        }
        # 最低限 raw を返す。以下は推測パース（壊れてても落ちない）
        if len(parts) >= 2:
            # 2番目: ISO8601 時刻
            stamp["time_utc"] = parts[1]
        if len(parts) >= 3:
            # 3番目: commit SHA
            stamp["commit_sha"] = parts[2]
        return stamp
    except FileNotFoundError:
        return {"raw": None, "time_utc": None, "commit_sha": None}
    except Exception as e:
        return {"raw": f"error: {e}", "time_utc": None, "commit_sha": None}

@app.get("/", response_class=PlainTextResponse)
def root():
    return "voicebot is running"

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/version")
def version():
    stamp = _read_deploy_stamp()
    # 参考情報（環境変数があれば一緒に返す）
    env = {
        "image_tag": os.getenv("IMAGE_TAG"),  # あれば
        "aws_region": os.getenv("AWS_REGION"),
    }
    return JSONResponse({
        "app": "voicebot",
        "deploy_stamp": stamp,   # {"raw": "...", "time_utc": "...", "commit_sha": "..."}
        "env": env
    })
