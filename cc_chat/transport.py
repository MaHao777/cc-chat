import asyncio
import os
from dataclasses import dataclass


@dataclass
class SendResult:
    status: str
    detail: str = ""


class MockTransport:
    def __init__(self):
        self.sent = []

    async def send(self, session, text):
        self.sent.append({"session": session, "text": text})
        return SendResult("sent", "模拟通道发送成功")


class WeixinTransport:
    def __init__(self, settings):
        self.settings = settings

    async def send(self, session, text):
        if not session or session != self.settings.peer_session:
            return SendResult("failed", "未绑定或不匹配的会话")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.settings.cc_binary,
                "send",
                "--project",
                self.settings.project,
                "--session",
                session,
                "--data-dir",
                str(self.settings.cc_data_dir),
                "--stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        except OSError:
            return SendResult("failed", "发送程序未启动")
        try:
            out, err = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SendResult("unknown", "通道超时，未自动重发")
        if proc.returncode == 0:
            return SendResult("sent")
        # CLI errors may occur after a platform accepted the message.
        return SendResult("unknown", f"通道返回码 {proc.returncode}，未自动重发")
