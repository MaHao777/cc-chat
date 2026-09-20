import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    _path: Path | None = PrivateAttr(default=None)
    data_dir: Path = ROOT / "data"
    runtime_dir: Path = ROOT / "runtime"
    port: int = 9881
    transport: str = "mock"
    project: str = "default"
    peer_session: str = ""
    cc_data_dir: Path = Path.home() / ".cc-connect"
    cc_binary: str = str(Path.home() / "AppData/Roaming/npm/node_modules/cc-connect/bin/cc-connect.exe")
    claude_binary: str = str(Path.home() / ".local/bin/claude.exe")
    model: str | None = None
    model_timeout: int = 180
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_local_only: bool = True
    weights: dict[str, float] = Field(
        default_factory=lambda: {"semantic": 0.45, "connection": 0.25, "keyword": 0.20, "time": 0.10}
    )
    compress_days: int = 30
    forget_days: int = 90
    low_importance: float = 0.4
    recent_days: int = 7
    recent_messages: int = 30
    # Wait for a short burst of user messages before asking the model to answer.
    message_wait_seconds: float = Field(default=10.0, ge=0, le=300)
    # Small pause between separate assistant bubbles when a burst is intentional.
    fragment_delay_seconds: float = Field(default=1.2, ge=0, le=30)
    context_chars: int = 18000
    memory_chars: int = 6000

    @classmethod
    def load(cls, path: Path | None = None):
        path = path or Path(os.getenv("CC_CHAT_CONFIG", ROOT / "config.local.json"))
        result = cls.model_validate(json.loads(path.read_text("utf-8"))) if path.exists() else cls()
        result._path = path
        return result

    def save(self, path: Path | None = None):
        path = path or self._path or ROOT / "config.local.json"
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        self._path = path
