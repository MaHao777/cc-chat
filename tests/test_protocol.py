"""Exercise the installed cc-connect binary with an isolated local bridge, never real WeChat."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import uvicorn
from conftest import PERSONA, Embedding, FakeModel

from cc_chat.config import Settings
from cc_chat.domain import Decision
from cc_chat.engine import Engine
from cc_chat.memory import Memory
from cc_chat.store import Store
from cc_chat.transport import WeixinTransport
from cc_chat.web import make_app


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(
    os.getenv("CC_CHAT_PROTOCOL_TEST") != "1", reason="opt-in installed cc-connect integration"
)
async def test_installed_cc_connect_end_to_end(tmp_path):
    import websockets

    # Windows AF_UNIX paths must fit sockaddr_un; pytest's default nested path is too long.
    short = tempfile.TemporaryDirectory(prefix="ccp-", dir=Path(__file__).resolve().parents[1] / ".cache")
    tmp_path = Path(short.name)
    api_port, bridge_port = free_port(), free_port()
    s = Settings(
        data_dir=tmp_path / "app",
        runtime_dir=tmp_path / "runtime",
        port=api_port,
        transport="weixin",
        project="smoke",
        peer_session="testchat:dm:owner",
        cc_data_dir=tmp_path / "cc",
    )
    s.runtime_dir.mkdir()
    settings_path = tmp_path / "settings.json"
    s.save(settings_path)
    db = Store(s.data_dir / "test.db")
    model = FakeModel()
    model.decisions = [
        Decision(action="reply", messages=["协议回复一"], reason="test", state="测试"),
        Decision(action="reply", messages=["协议回复二"], reason="test", state="测试"),
        Decision(action="reply", messages=["协议主动联系"], reason="test", state="测试"),
    ]
    db.save_persona(PERSONA)
    engine = Engine(db, s, model, Memory(db, s, Embedding()), WeixinTransport(s))
    server = uvicorn.Server(
        uvicorn.Config(make_app(engine), host="127.0.0.1", port=api_port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    config_path = tmp_path / "cc.toml"
    import tomlkit

    config_path.write_text(
        tomlkit.dumps(
            {
                "data_dir": str(s.cc_data_dir),
                "log": {"level": "error"},
                "display": {
                    "thinking_messages": False,
                    "tool_messages": False,
                    "show_context_indicator": False,
                    "reply_footer": False,
                },
                "bridge": {"enabled": True, "port": bridge_port, "token": "local-test-only"},
                "projects": [
                    {
                        "name": "smoke",
                        "platforms": [
                            {
                                "type": "weixin",
                                "options": {
                                    "token": "local-test-never-real",
                                    "base_url": "http://127.0.0.1:9",
                                    "allow_from": "owner",
                                },
                            }
                        ],
                        "agent": {
                            "type": "claudecode",
                            "options": {
                                "work_dir": str(s.runtime_dir),
                                "cli_path": str(Path(sys.executable).resolve()) + " -m cc_chat.adapter",
                                "env": {"CC_CHAT_CONFIG": str(settings_path)},
                            },
                        },
                    }
                ],
            }
        ),
        "utf-8",
    )
    log_path = tmp_path / "cc.log"
    proc = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                [s.cc_binary, "--config", str(config_path)],
                stdout=log,
                stderr=log,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        ws = None
        for _ in range(60):
            if proc.poll() is not None:
                pytest.fail(log_path.read_text("utf-8", errors="replace"))
            try:
                ws = await websockets.connect(
                    f"ws://127.0.0.1:{bridge_port}/bridge/ws?token=local-test-only", proxy=None
                )
                break
            except OSError:
                await asyncio.sleep(0.2)
        assert ws is not None
        async with ws:
            await ws.send(
                json.dumps(
                    {"type": "register", "platform": "testchat", "project": "smoke", "capabilities": ["text"]}
                )
            )
            ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert ack["type"] == "register_ack", ack
            replies = []
            for number in [1, 2]:
                await ws.send(
                    json.dumps(
                        {
                            "type": "message",
                            "msg_id": f"msg-{number}",
                            "session_key": s.peer_session,
                            "user_id": "owner",
                            "user_name": "test",
                            "content": f"测试 {number}",
                            "reply_ctx": "local-test",
                        }
                    )
                )
                while True:
                    event = json.loads(await asyncio.wait_for(ws.recv(), 25))
                    if event["type"] == "reply":
                        replies.append(event)
                        break
            db.add_job("proactive", engine.clock(), {"plan": {"intent": "协议主动测试"}})
            while len(replies) < 3:
                event = json.loads(await asyncio.wait_for(ws.recv(), 25))
                if event["type"] == "reply":
                    replies.append(event)
            texts = [x.get("content", x.get("text")) for x in replies]
            assert texts == ["协议回复一", "协议回复二", "协议主动联系"], replies
            for _ in range(40):
                if len(db.rows("SELECT * FROM messages WHERE role='assistant'")) == 3:
                    break
                await asyncio.sleep(0.05)
            assert len(db.rows("SELECT * FROM messages WHERE role='user'")) == 2
            assert len(db.rows("SELECT * FROM messages WHERE role='assistant'")) == 3
            assert all("NO_REPLY" not in json.dumps(r) for r in replies)
            try:
                extra = await asyncio.wait_for(ws.recv(), 2)
                assert json.loads(extra)["type"] != "reply", extra
            except TimeoutError:
                pass
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 10)
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        short.cleanup()
