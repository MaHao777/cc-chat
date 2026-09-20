"""Minimal cc-connect claudecode stream-json ingress. No generated text goes to stdout."""

import json
import os
import sys
import urllib.error
import urllib.request
from uuid import uuid4

from .config import Settings


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def post(settings, path, payload):
    secret = (settings.data_dir / "access.token").read_text("utf-8").strip()
    req = urllib.request.Request(
        f"http://127.0.0.1:{settings.port}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as res:
        return json.load(res)


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.load()
    session = os.getenv("CC_SESSION_KEY", "")
    project = os.getenv("CC_PROJECT", "")
    if (
        project != settings.project
        or not session
        or (settings.peer_session and session != settings.peer_session)
    ):
        print("cc-chat: project/session not bound", file=sys.stderr)
        raise SystemExit(2)
    args = sys.argv[1:]
    sid = str(uuid4())
    if "--resume" in args:
        pos = args.index("--resume") + 1
        if pos < len(args):
            sid = args[pos]
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "cc-chat"})
    spool_dir = settings.data_dir / "inbox"
    spool_dir.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        try:
            raw = json.loads(line)
            if raw.get("type") == "control_request":
                if raw.get("request", {}).get("subtype") == "interrupt":
                    post(settings, "/internal/cancel", {"session": session})
                emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": raw.get("request_id"),
                            "response": {},
                        },
                    }
                )
                continue
            if raw.get("type") != "user":
                continue
            content = raw.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            if not isinstance(content, str) or not content.strip():
                content = "[收到非文字消息，当前版本仅支持文字，请自然说明无法读取附件，不编造附件内容。]"
            payload = {"session": session, "content": content, "source_key": raw.get("uuid") or str(uuid4())}
            spool = spool_dir / (str(uuid4()) + ".json")
            temp = spool.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(spool)
            try:
                post(settings, "/internal/incoming", payload)
                spool.unlink(missing_ok=True)
            except (OSError, urllib.error.URLError):
                print("cc-chat: backend unavailable; incoming message durably spooled", file=sys.stderr)
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": sid,
                    "result": "NO_REPLY",
                    "num_turns": 1,
                    "duration_ms": 0,
                    "duration_api_ms": 0,
                    "total_cost_usd": 0,
                }
            )
        except (ValueError, TypeError):
            print("cc-chat: malformed input event", file=sys.stderr)


if __name__ == "__main__":
    main()
