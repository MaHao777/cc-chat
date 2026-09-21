import asyncio
from datetime import timedelta

from conftest import BRIEF, PERSONA

from cc_chat.domain import Activity, DaySchedule, Decision, Plan, iso, uid
from cc_chat.store import dump
from cc_chat.transport import SendResult


async def test_one_reply_and_dedup(env):
    e, db, model, memory, transport, clock = env
    first = await e.receive("mock:owner", "你好", "wx-1")
    repeat = await e.receive("mock:owner", "你好", "wx-1")
    assert first == repeat
    await e.tick()
    assert len(transport.sent) == 1
    assert len(db.rows("SELECT * FROM messages WHERE role='user'")) == 1
    assert len(db.rows("SELECT * FROM messages WHERE role='assistant'")) == 1


async def test_identical_text_new_ids_not_deduplicated(env):
    e, db, *_ = env
    await e.receive("mock:owner", "好", "a")
    await e.receive("mock:owner", "好", "b")
    assert len(db.rows("SELECT * FROM messages")) == 2


async def test_user_message_wait_timer_resets_and_combines_messages(env):
    e, db, model, memory, transport, clock = env
    e.settings.message_wait_seconds = 10
    await e.receive("mock:owner", "第一句", "a")
    first_due = db.one("SELECT due FROM jobs WHERE kind='turn' AND status='pending'")["due"]
    assert first_due == iso(clock() + timedelta(seconds=10))
    clock.now += timedelta(seconds=6)
    await e.receive("mock:owner", "第二句", "b")
    turns = db.rows("SELECT * FROM jobs WHERE kind='turn' AND status='pending'")
    assert len(turns) == 1
    assert turns[0]["due"] == iso(clock() + timedelta(seconds=10))
    await e.tick()
    assert not [call for call in model.calls if call[0] == "decision"]
    clock.now += timedelta(seconds=11)
    await e.tick()
    decision_context = [call[1] for call in model.calls if call[0] == "decision"][-1]
    assert [m["content"] for m in decision_context["recent_messages"]][-2:] == ["第一句", "第二句"]


async def test_normal_turn_is_compact_but_worthwhile_share_can_burst(env):
    e, db, model, memory, transport, clock = env
    model.decisions.append(
        Decision(
            action="reply",
            messages=["一", "二", "三", "四"],
            reason="普通回应",
            state="空闲",
        )
    )
    await e.receive("mock:owner", "你好", "normal")
    await e.tick()
    assert [item["text"] for item in transport.sent] == ["一", "二"]
    model.decisions.append(
        Decision(
            action="reply",
            messages=["甲", "乙", "丙", "丁"],
            message_mode="share",
            reason="值得分享",
            state="兴奋",
        )
    )
    await e.receive("mock:owner", "还有个故事", "share")
    await e.tick()
    assert [item["text"] for item in transport.sent][-4:] == ["甲", "乙", "丙", "丁"]


async def test_intentional_fragments_are_spaced(env, monkeypatch):
    e, db, model, memory, transport, clock = env
    e.settings.fragment_delay_seconds = 1.2
    model.decisions.append(
        Decision(action="reply", messages=["先说这个", "再补一句"], reason="自然分段", state="空闲")
    )
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("cc_chat.engine.asyncio.sleep", fake_sleep)
    await e.receive("mock:owner", "你好", "spaced")
    await e.tick()
    assert 1.2 in pauses


async def test_new_user_message_invalidates_inflight_decision(env):
    e, db, model, memory, transport, clock = env
    await e.receive("mock:owner", "我想喝咖啡", "a")

    async def arrive():
        await e.receive("mock:owner", "等等，改喝茶", "b")

    model.before_decision = arrive
    await e.tick()
    assert transport.sent == []
    await e.tick()
    assert len(transport.sent) == 1
    assert model.calls[-1][1]["recent_messages"][-1]["content"] == "等等，改喝茶"


async def test_deferred_reply_is_reconsidered_not_stale_text(env):
    e, db, model, memory, transport, clock = env
    due = clock() + timedelta(hours=1)
    model.decisions.append(Decision(action="defer", reply_at=iso(due), reason="在上课", state="上课中"))
    await e.receive("mock:owner", "下课聊", "a")
    await e.tick()
    assert not transport.sent
    assert db.one("SELECT * FROM jobs WHERE kind='reply'")["due"] == iso(due)
    clock.now = due
    await e.tick()
    assert len(transport.sent) == 1
    assert model.calls[-1][1]["trigger"] == "reply"


async def test_proactive_timing_has_no_daily_quota(env):
    e, db, model, memory, transport, clock = env
    await e.receive("mock:owner", "有趣的事可以分享", "a")
    for i in range(12):
        model.decisions.append(
            Decision(
                action="reply",
                messages=[f"分享 {i}"],
                state="散步",
                reason="想到话题",
                next_plan=Plan(
                    at=iso(clock() + timedelta(seconds=1)),
                    intent="想到一件事",
                    condition="重新判断",
                    reason="想分享",
                ),
            )
        )
        await e.tick()
        clock.now += timedelta(seconds=2)
    assert len(transport.sent) == 12


async def test_pause_blocks_proactive_but_allows_direct_reply(env):
    e, db, model, memory, transport, clock = env
    await e.receive("mock:owner", "你好", "a")
    await e.tick()
    db.add_job("proactive", clock(), {"plan": {"intent": "打招呼"}})
    await e.pause(True)
    await e.tick()
    assert len(transport.sent) == 1
    await e.receive("mock:owner", "现在可以聊", "b")
    await e.tick()
    assert len(transport.sent) == 2


async def test_new_message_between_fragments_cancels_rest(env):
    e, db, model, memory, transport, clock = env
    model.decisions.append(
        Decision(action="reply", messages=["第一条", "第二条"], reason="自然分段", state="空闲")
    )
    first_sent = asyncio.Event()
    original = transport.send

    async def send(session, text):
        result = await original(session, text)
        first_sent.set()
        await asyncio.sleep(0)
        return result

    transport.send = send
    await e.receive("mock:owner", "你好", "a")

    async def interrupt():
        await first_sent.wait()
        await e.receive("mock:owner", "先等等", "b")

    await asyncio.gather(e.tick(), interrupt())
    assert [m["text"] for m in transport.sent] == ["第一条"]


async def test_unknown_send_not_in_history_or_retried(env):
    e, db, model, memory, transport, clock = env

    async def uncertain(*args):
        return SendResult("unknown", "timeout")

    transport.send = uncertain
    await e.receive("mock:owner", "你好", "a")
    await e.tick()
    assert db.one("SELECT * FROM outbox")["status"] == "unknown"
    assert not db.rows("SELECT * FROM messages WHERE role='assistant'")
    db.recover()
    await e.flush()
    assert len(db.rows("SELECT * FROM outbox")) == 1


async def test_recovery_marks_inflight_send_unknown(env):
    e, db, model, memory, transport, clock = env
    db.execute(
        "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?)",
        (uid(), "mock:owner", "可能发过", "sending", 1, 1, iso(clock()), "", "[]"),
    )
    db.recover()
    assert db.one("SELECT * FROM outbox")["status"] == "unknown"
    await e.flush()
    assert not transport.sent


async def test_overdue_proactive_gets_latest_time_once(env):
    e, db, model, memory, transport, clock = env
    db.set("session", "mock:owner")
    db.add_job("proactive", clock() - timedelta(days=2), {"plan": {"intent": "旧计划"}})
    await e.tick()
    await e.tick()
    assert len(transport.sent) == 1
    assert next(c[1] for c in model.calls if c[0] == "decision")["now"] == iso(clock())


async def test_seven_days_life_no_future_as_past(env):
    e, db, model, memory, transport, clock = env
    for day in range(7):
        morning = clock()
        model.schedules.append(
            DaySchedule(
                activities=[
                    Activity(
                        at=iso(morning + timedelta(hours=1)),
                        until=iso(morning + timedelta(hours=2)),
                        title="文学课",
                        outline="学习",
                    )
                ]
            )
        )
        await e.life()
        assert db.one("SELECT COUNT(*) n FROM events WHERE status='occurred'")["n"] == day
        ctx = await e.context("学习", "test")
        assert all(x["at"] <= iso(clock()) for x in ctx["occurred_events"])
        clock.now += timedelta(hours=2)
        await e.life()
        clock.now = morning.replace(hour=15, minute=35)
        await e.life()
        await e.life()
        assert db.one("SELECT COUNT(*) n FROM diaries")["n"] == day + 1
        clock.now = morning + timedelta(days=1)
    assert not transport.sent


async def test_diary_addendum_only_new_sources(env):
    e, db, model, memory, transport, clock = env
    clock.now = clock().replace(hour=15, minute=35)
    await e.receive("mock:owner", "睡前聊一下", "a")
    await e.write_diary("2026-09-19", clock())
    await e.receive("mock:owner", "还有一件事", "b")
    await e.write_diary("2026-09-19", clock())
    calls = [c[1] for c in model.calls if c[0] == "diary"]
    assert len(calls) == 2 and calls[-1]["is_addendum"]
    assert [m["content"] for m in calls[-1]["messages"]] == ["还有一件事"]


async def test_model_error_never_creates_assistant_message(env):
    e, db, model, memory, transport, clock = env

    async def fail(*args):
        raise RuntimeError("sensitive-provider-details")

    model.generate = fail
    await e.receive("mock:owner", "你好", "a")
    await e.tick()
    assert not transport.sent
    assert "sensitive-provider-details" not in dump(db.get("last_error"))
    assert db.get("last_error")["type"] == "RuntimeError"


async def test_initial_persona_does_not_discard_first_message(env):
    e, db, model, memory, transport, clock = env
    db.execute("DELETE FROM personas")
    e.settings.persona_brief = BRIEF
    await e.receive("mock:owner", "初次见面", "first")
    await e.tick()
    assert len(transport.sent) == 1


async def test_missing_persona_is_never_invented(env):
    e, db, model, memory, transport, clock = env
    db.execute("DELETE FROM personas")
    await e.tick()
    assert db.persona() is None
    assert not [task for task, _ in model.calls if task == "persona"]
    assert "还没有角色设定" in db.get("state")


async def test_persona_brief_is_the_only_source_of_the_character(env):
    e, db, model, memory, transport, clock = env
    db.execute("DELETE FROM personas")
    e.settings.persona_brief = BRIEF
    await e.tick()
    assert db.persona()["identity"] == PERSONA["identity"]
    brief = [context for task, context in model.calls if task == "persona"][0]
    assert brief["brief"] == BRIEF
    assert "school" not in brief and "persona" not in brief


async def test_null_next_plan_cancels_previous_intent(env):
    e, db, model, memory, transport, clock = env
    db.set("session", "mock:owner")
    db.add_job("reply", clock(), {})
    db.add_job("proactive", clock() + timedelta(hours=2), {"plan": {"intent": "已过时"}})
    model.decisions.append(Decision(action="wait", reason="现在不想联系", state="休息", next_plan=None))
    await e.tick()
    assert not db.rows("SELECT * FROM jobs WHERE status='pending'")


async def test_whole_context_budget_keeps_latest_user(env):
    e, db, model, memory, transport, clock = env
    for i in range(30):
        await e.receive("mock:owner", "较长的旧消息" * 200, str(i))
    await e.receive("mock:owner", "最新消息开头" + "长内容" * 5000 + "最新消息末尾", "last")
    ctx = await e.context("最新消息", "test")
    assert len(dump(ctx)) <= e.settings.context_chars
    assert ctx["recent_messages"][-1]["content"].startswith("最新消息开头")


async def test_invalid_plan_does_not_send_partial_effects(env):
    e, db, model, memory, transport, clock = env
    model.decisions.append(
        Decision(
            action="reply",
            messages=["不应发出"],
            reason="bad",
            state="空闲",
            next_plan=Plan(
                at=iso(clock() - timedelta(hours=1)), intent="非法过去时间", condition="", reason=""
            ),
        )
    )
    await e.receive("mock:owner", "你好", "a")
    await e.tick()
    assert not transport.sent and not db.rows("SELECT * FROM outbox")
