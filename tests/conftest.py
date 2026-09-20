import json
from datetime import datetime, timezone

import pytest

from cc_chat.config import Settings
from cc_chat.domain import CompressedMemory, DaySchedule, Decision, Diary, Experience, Persona
from cc_chat.engine import Engine
from cc_chat.memory import Memory
from cc_chat.store import Store
from cc_chat.transport import MockTransport

PERSONA = dict(
    name="林知夏",
    age=20,
    school="云川大学（虚构）",
    major="中文",
    year="大二",
    personality="安静但有主见",
    speaking_style="自然简短",
    interests=["阅读"],
    background="在中国读大学",
    courses=["文学史"],
    people=["室友小禾（虚构）"],
)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 19, 2, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Embedding:
    async def encode(self, texts):
        output = []
        for text in texts:
            if any(t in text for t in ("咖啡", "拿铁", "饮品")):
                output.append([1.0, 0.0, 0.0])
            elif any(t in text for t in ("课", "学习", "考试")):
                output.append([0.0, 1.0, 0.0])
            else:
                output.append([0.0, 0.0, 1.0])
        return output


class FakeModel:
    def __init__(self):
        self.decisions = []
        self.calls = []
        self.before_decision = None
        self.schedules = []

    async def generate(self, task, context, schema):
        self.calls.append((task, json.loads(json.dumps(context))))
        if task == "persona":
            return Persona(**PERSONA)
        if task == "decision":
            if self.before_decision:
                await self.before_decision()
                self.before_decision = None
            return (
                self.decisions.pop(0)
                if self.decisions
                else Decision(action="reply", messages=["嗯，我在呢。"], reason="回应", state="在宿舍")
            )
        if task == "schedule":
            return self.schedules.pop(0) if self.schedules else DaySchedule(activities=[])
        if task == "experience":
            return Experience(content="参加了这次活动。", state="刚下课", memories=[])
        if task == "diary":
            return Diary(content="今天平平淡淡，也有值得记住的片刻。")
        if task == "compress":
            return CompressedMemory(content="曾经聊过一次饮品喜好。")
        raise AssertionError(task)


@pytest.fixture
def env(tmp_path):
    # Unit tests drive the clock manually; production defaults keep a 10-second
    # message debounce and a visible pause between intentional fragments.
    s = Settings(
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        message_wait_seconds=0,
        fragment_delay_seconds=0,
    )
    db = Store(s.data_dir / "test.db")
    model, clock, transport = FakeModel(), Clock(), MockTransport()
    memory = Memory(db, s, Embedding())
    e = Engine(db, s, model, memory, transport, clock)
    db.save_persona(PERSONA, clock())
    return e, db, model, memory, transport, clock
