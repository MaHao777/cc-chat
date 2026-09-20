from datetime import timedelta

from cc_chat.domain import Link, MemoryDraft, iso, uid
from cc_chat.store import dump


async def remember(
    env, content="我喜欢不加糖的咖啡", source_type="user", importance=0.2, kind="preference", **kwargs
):
    e, db, model, mem, transport, clock = env
    mid = uid()
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,1,?)",
        (mid, "mock:owner", "user", content, iso(clock()), db.get("revision"), mid),
    )
    draft = MemoryDraft(
        content=content,
        subject="用户",
        kind=kind,
        source_type=source_type,
        source_ids=[mid],
        occurred_at=iso(clock()),
        importance=importance,
        confidence=1,
        **kwargs,
    )
    key = await mem.add(draft, [mid], clock())
    return key, mid


async def test_semantic_synonym_and_irrelevant_recency(env):
    e, db, model, mem, transport, clock = env
    key, _ = await remember(env)
    clock.now += timedelta(days=10)
    await remember(env, "今天课上的考试很好", importance=0.8)
    found = await mem.search("平时喜欢哪种拿铁饮品", clock())
    assert found[0]["id"] == key
    assert len(found) == 1


async def test_explicit_link_expands_recall(env):
    e, db, model, mem, transport, clock = env
    coffee, _ = await remember(env)
    related, _ = await remember(
        env, "考试前会去图书馆", links=[Link(target=coffee, kind="event", strength=0.9)]
    )
    found = await mem.search("拿铁", clock())
    row = next(m for m in found if m["id"] == related)
    assert row["scores"]["connection"] > 0
    assert row["scores"]["paths"] == [{"from": coffee, "kind": "event"}]


async def test_retrieval_alone_does_not_reinforce(env):
    e, db, model, mem, transport, clock = env
    key, _ = await remember(env)
    old = db.one("SELECT last_used FROM memories WHERE id=?", (key,))["last_used"]
    clock.now += timedelta(days=12)
    await mem.search("拿铁", clock())
    assert db.one("SELECT last_used FROM memories WHERE id=?", (key,))["last_used"] == old
    mem.reinforce([key], clock())
    assert db.one("SELECT last_used FROM memories WHERE id=?", (key,))["last_used"] == iso(clock())


async def test_correction_supersedes_old_and_hides_raw_source(env):
    e, db, model, mem, transport, clock = env
    key, mid = await remember(env)
    new, _ = await remember(env, "我现在只喝无咖啡因的咖啡", supersedes=key, explicit_correction=True)
    found = await mem.search("咖啡", clock())
    assert key not in {m["id"] for m in found} and new in {m["id"] for m in found}
    assert db.one("SELECT visible FROM messages WHERE id=?", (mid,))["visible"] == 0


async def test_inference_cannot_replace_user_fact(env):
    e, db, model, mem, transport, clock = env
    key, _ = await remember(env)
    rejected, _ = await remember(
        env,
        "猜测用户不喝咖啡",
        source_type="inference",
        kind="inference",
        supersedes=key,
        explicit_correction=True,
    )
    assert rejected is None
    assert db.one("SELECT status FROM memories WHERE id=?", (key,))["status"] == "active"


async def test_virtual_memory_requires_real_event_source(env):
    e, db, model, mem, transport, clock = env
    key, _ = await remember(env, "我今天去上课了", source_type="virtual")
    assert key is None


async def test_compression_blocks_chat_diary_and_derived_message(env):
    e, db, model, mem, transport, clock = env
    key, mid = await remember(env, "9月19日在一号店喝了38元拿铁")
    aid = uid()
    db.execute(
        "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, "mock:owner", "你喝了38元拿铁", "sent", 1, 0, iso(clock()), "", dump([key])),
    )
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,1,?)",
        (aid, "mock:owner", "assistant", "你喝了38元拿铁", iso(clock()), 1, aid),
    )
    db.execute(
        "INSERT INTO diaries VALUES (?,?,?,?,?,1)",
        (uid(), "2026-09-19", iso(clock()), "记得38元拿铁", dump([mid])),
    )
    db.execute("UPDATE memories SET last_used=? WHERE id=?", (iso(clock() - timedelta(days=31)), key))
    await mem.maintain(model, clock())
    ctx = await e.context("拿铁", "test")
    text = dump(ctx)
    assert "38元" not in text and "一号店" not in text
    assert "38元" in db.one("SELECT content FROM memories WHERE id=?", (key,))["content"]
    assert db.one("SELECT vector FROM memories WHERE id=?", (key,))["vector"] is None
    summary = db.one("SELECT * FROM memories WHERE summary_of=?", (key,))
    assert summary and summary["status"] == "active"


async def test_forgetting_removes_graph_path_and_keeps_archive(env):
    e, db, model, mem, transport, clock = env
    old, _ = await remember(env, "昨晚在考试前喝过咖啡")
    await remember(env, "我常去图书馆", importance=0.8, links=[Link(target=old, kind="event", strength=1)])
    clock.now += timedelta(days=91)
    await mem.maintain(model, clock())
    found = await mem.search("图书馆", clock())
    assert old not in {m["id"] for m in found}
    assert db.one("SELECT status FROM memories WHERE id=?", (old,))["status"] == "forgotten"


async def test_pinned_and_unfinished_promises_survive(env):
    e, db, model, mem, transport, clock = env
    key, _ = await remember(env)
    db.execute("UPDATE memories SET pinned=1 WHERE id=?", (key,))
    promise, _ = await remember(env, "一起准备考试", kind="promise", unresolved=True)
    clock.now += timedelta(days=150)
    await mem.maintain(model, clock())
    assert {m["id"] for m in mem.available()} == {key, promise}


async def test_deleted_history_not_reintroduced_by_diary(env):
    e, db, model, mem, transport, clock = env
    key, mid = await remember(env)
    mem.retire(db.one("SELECT * FROM memories WHERE id=?", (key,)), "deleted")
    await e.write_diary("2026-09-19", clock())
    assert not [c for c in model.calls if c[0] == "diary"]


async def test_delete_erases_diary_and_retrieval_snapshots(env):
    e, db, model, mem, transport, clock = env
    key, mid = await remember(env, "最爱38元咖啡")
    db.execute(
        "INSERT INTO diaries VALUES (?,?,?,?,?,1)",
        (uid(), "2026-09-19", iso(clock()), "最爱38元咖啡", dump([mid])),
    )
    await mem.search("38元咖啡", clock())
    mem.delete(db.one("SELECT * FROM memories WHERE id=?", (key,)))
    assert "38元" not in dump(db.rows("SELECT * FROM memories"))
    assert "38元" not in dump(db.rows("SELECT * FROM diaries"))
    assert "38元" not in dump(db.rows("SELECT * FROM retrievals"))
