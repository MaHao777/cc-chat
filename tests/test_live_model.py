"""Opt-in real Claude/local-embedding smoke. All conversation data is synthetic and temporary."""

import os

import pytest
from conftest import PERSONA

from cc_chat.config import Settings
from cc_chat.domain import Decision, iso, utcnow
from cc_chat.memory import LocalEmbedder
from cc_chat.model import ClaudeModel


@pytest.mark.skipif(os.getenv("CC_CHAT_LIVE_TEST") != "1", reason="opt-in real Claude Code call")
async def test_real_claude_structured_decision(tmp_path):
    settings = Settings(runtime_dir=tmp_path / "model", data_dir=tmp_path / "data")
    result = await ClaudeModel(settings).generate(
        "decision",
        {
            "now": iso(utcnow()),
            "persona": PERSONA,
            "trigger": "turn",
            "state": "周末空闲，在宿舍看书",
            "recent_messages": [
                {
                    "id": "synthetic-user-1",
                    "role": "user",
                    "content": "你好！我平时喜欢喝不加糖的拿铁。你在做什么？",
                }
            ],
            "memories": [],
            "occurred_events": [],
            "future_schedule": [],
        },
        Decision,
    )
    assert result.action in {"reply", "defer", "wait", "cancel"}
    assert result.state
    for memory in result.memories:
        assert set(memory.source_ids) <= {"synthetic-user-1"}


@pytest.mark.skipif(os.getenv("CC_CHAT_LIVE_TEST") != "1", reason="opt-in actual embedding model")
async def test_real_chinese_embeddings(tmp_path):
    vectors = await LocalEmbedder(Settings()).encode(
        ["我最喜欢不加糖的咖啡", "我爱喝无糖拿铁", "明天有数学考试"]
    )

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b))

    assert len(vectors[0]) == 512
    assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2]) + 0.15
