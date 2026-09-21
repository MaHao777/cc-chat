import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import DaySchedule, Decision, Diary, Experience, Persona, iso, parse, uid, utcnow
from .store import dump

SHANGHAI = ZoneInfo("Asia/Shanghai")


class Engine:
    def __init__(self, store, settings, model, memory, transport, clock=utcnow):
        self.db, self.settings, self.model = store, settings, model
        self.memory, self.transport, self.clock = memory, transport, clock
        self.gate = asyncio.Lock()
        self.tick_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.model_failures = 0
        self.cooldown = None

    async def initialize(self):
        if self.db.persona():
            return
        brief = self.settings.persona_brief.strip()
        if not brief:
            # Never invent a character on the user's behalf; wait for one to be described.
            return
        context = {"now": iso(self.clock()), "brief": brief}
        persona = await self.model.generate("persona", context, Persona)
        self.db.save_persona(persona.model_dump(), self.clock())

    async def receive(self, session, content, source_key):
        if self.settings.transport == "weixin" and session != self.settings.peer_session:
            raise ValueError("只接受已绑定用户")
        if not content.strip() or len(content) > 20000:
            raise ValueError("消息不能为空或超过 20000 字符")
        async with self.gate:
            prior = self.db.one("SELECT id,revision FROM messages WHERE source_key=?", (source_key,))
            if prior:
                return prior
            now, mid = self.clock(), uid()
            with self.db.connect() as c:
                rev = self.db.get("revision") + 1
                old = self.db.rows(
                    "SELECT kind,due,payload FROM jobs WHERE status='pending' AND kind IN ('proactive','reply')"
                )
                c.execute("UPDATE meta SET value=? WHERE key='revision'", (dump(rev),))
                c.execute("INSERT OR REPLACE INTO meta VALUES ('session',?)", (dump(session),))
                c.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?,1,?)",
                    (mid, session, "user", content, iso(now), rev, source_key),
                )
                c.execute(
                    "UPDATE jobs SET status='cancelled' WHERE status='pending' AND kind IN ('turn','reply','proactive')"
                )
                c.execute("UPDATE outbox SET status='cancelled' WHERE status='pending'")
            # Debounce a short burst of user messages. Each new message
            # cancels the previous turn and starts this timer again.
            self.db.add_job(
                "turn",
                now + timedelta(seconds=self.settings.message_wait_seconds),
                {"message_id": mid, "old_plans": old},
                rev,
            )
            return {"id": mid, "revision": rev}

    async def pause(self, value):
        async with self.gate:
            self.db.set("paused", value)
            if value:
                self.db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND proactive=1")
        self.db.audit("proactive.pause", {"paused": value})

    async def context(self, query, trigger, payload=None):
        now = self.clock()
        memories = await self.memory.search(query, now)
        cutoff = iso(now - timedelta(days=self.settings.recent_days))
        messages = self.db.rows(
            "SELECT id,role,content,at FROM messages WHERE visible=1 AND at>=? ORDER BY at DESC,rowid DESC LIMIT ?",
            (cutoff, self.settings.recent_messages),
        )
        budget = max(1000, self.settings.context_chars - self.settings.memory_chars - 5000)
        selected, size = [], 0
        for msg in messages:
            if not self.memory.safe_sources([msg["id"]]):
                continue
            if not selected and len(msg["content"]) > budget:
                msg["content"] = (
                    msg["content"][: budget // 2] + "〔长消息中间已截断〕" + msg["content"][-budget // 2 :]
                )
            if selected and size + len(msg["content"]) > budget:
                continue
            selected.append(msg)
            size += len(msg["content"])
        events = [
            e
            for e in self.db.rows(
                "SELECT id,at,title,content FROM events WHERE status='occurred' AND at>=? AND at<=? ORDER BY at DESC LIMIT 8",
                (cutoff, iso(now)),
            )
            if self.memory.safe_sources([e["id"]])
        ]
        diaries = [
            d
            for d in self.db.rows(
                "SELECT * FROM diaries WHERE visible=1 AND created>=? ORDER BY created DESC LIMIT 2",
                (cutoff,),
            )
            if self.memory.safe_sources(json.loads(d["source_ids"]))
        ]
        ctx = {
            "now": iso(now),
            "local_time": now.astimezone(SHANGHAI).isoformat(),
            "persona": self.db.persona(),
            "state": self.db.get("state"),
            "trigger": trigger,
            "recent_messages": list(reversed(selected)),
            "memories": memories,
            "occurred_events": events,
            "recent_diaries": [{"content": d["content"][:1500], "day": d["day"]} for d in diaries],
            "future_schedule": self.db.rows(
                "SELECT id,at,until_at,title FROM events WHERE status='planned' ORDER BY at LIMIT 8"
            ),
            "old_plans": (payload or {}).get("old_plans", []),
            "plan": (payload or {}).get("plan"),
        }
        # Enforce the total serialized budget, not only each individual section's budget.
        for key in ("recent_diaries", "occurred_events", "future_schedule", "old_plans"):
            while len(dump(ctx)) > self.settings.context_chars and ctx[key]:
                ctx[key].pop()
        while len(dump(ctx)) > self.settings.context_chars and len(ctx["recent_messages"]) > 1:
            ctx["recent_messages"].pop(0)
        while len(dump(ctx)) > self.settings.context_chars and ctx["memories"]:
            ctx["memories"].pop()
        if len(dump(ctx)) > self.settings.context_chars:
            # User-editable personality/background can be very large. Trim strings only, preserving shape.
            def shorten(value, cap):
                if isinstance(value, str):
                    return value[:cap]
                if isinstance(value, list):
                    return [shorten(v, cap) for v in value[:20]]
                if isinstance(value, dict):
                    return {k: shorten(v, cap) for k, v in value.items()}
                return value

            for cap in (2000, 1000, 500, 200):
                ctx = shorten(ctx, cap)
                if len(dump(ctx)) <= self.settings.context_chars:
                    break
        return ctx

    async def decide(self, job):
        rev = job["revision"]
        proactive = job["kind"] == "proactive"
        if rev != self.db.get("revision") or (proactive and self.db.get("paused")):
            return
        payload = json.loads(job["payload"])
        latest = self.db.one(
            "SELECT content FROM messages WHERE role='user' AND visible=1 ORDER BY at DESC,rowid DESC LIMIT 1"
        )
        query = latest["content"] if latest else "今天的生活以及值得分享的事情"
        if payload.get("plan"):
            query += " " + payload["plan"]["intent"]
        ctx = await self.context(query, job["kind"], payload)
        result = await self.model.generate("decision", ctx, Decision)
        now = self.clock()
        # Validate the entire decision before persisting or sending any effects.
        if result.action == "defer" and (not result.reply_at or parse(result.reply_at) <= now):
            raise ValueError("延迟回复必须设置未来时间")
        if result.action == "reply" and result.reply_at:
            raise ValueError("即时回复不能指定未来时间")
        if result.next_plan and parse(result.next_plan.at) <= now:
            raise ValueError("下一次计划必须位于未来")
        if any(len(t) > 3000 or t.strip() == "NO_REPLY" for t in result.messages):
            raise ValueError("回复超长或包含协议标记")
        if rev != self.db.get("revision") or (proactive and self.db.get("paused")):
            self.db.audit("decision.stale", {"job": job["id"]})
            return
        available = {m["id"] for m in ctx["memories"]}
        source_ids = {m["id"] for m in ctx["recent_messages"]}
        source_ids.update(m["id"] for m in ctx["occurred_events"])
        for draft in result.memories:
            await self.memory.add(draft, source_ids, now)
        if rev != self.db.get("revision"):
            return
        # Each completed decision replaces the previous future intent, including an explicit null plan.
        self.db.execute(
            "UPDATE jobs SET status='cancelled' WHERE status='pending' AND kind IN ('reply','proactive')"
        )
        self.memory.reinforce(set(result.confirmed_memory_ids) & available, now)
        for i in set(result.resolved_memory_ids) & available:
            self.db.execute("UPDATE memories SET unresolved=0 WHERE id=?", (i,))
        self.db.set("state", result.state[:300])
        self.db.audit(
            "decision",
            {
                "trigger": job["kind"],
                "action": result.action,
                "reason": result.reason,
                "revision": rev,
                "used_memory_ids": sorted(set(result.used_memory_ids) & available),
            },
        )
        if result.change_future_schedule:
            self.db.execute("UPDATE events SET status='cancelled' WHERE status='planned'")
            self.db.set("schedule_day", None)
        if result.action == "defer":
            if not result.reply_at or parse(result.reply_at) <= now:
                raise ValueError("延迟回复必须设置未来时间")
            self.db.add_job(
                "reply",
                parse(result.reply_at),
                {
                    "plan": {
                        "intent": "回应尚未回复的用户消息",
                        "reason": result.reason,
                        "condition": "结合最新状态重新判断",
                        "memory_ids": [],
                        "at": result.reply_at,
                    }
                },
                rev,
            )
        elif result.action == "reply":
            if result.reply_at:
                raise ValueError("即时回复不能指定未来发送时间；请使用 defer")
            # Keep ordinary turns natural and compact. A burst is an explicit
            # model decision reserved for something genuinely worth sharing.
            max_messages = 5 if result.message_mode == "share" else 2
            messages = [text for text in result.messages if text.strip()][:max_messages]
            if len(result.messages) > max_messages:
                self.db.audit(
                    "decision.messages_trimmed",
                    {"mode": result.message_mode, "kept": max_messages},
                )
            for text in messages:
                text = text.strip()
                if not text:
                    continue
                if len(text) > 3000 or text == "NO_REPLY":
                    raise ValueError("回复超长或包含协议标记")
                self.db.execute(
                    "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        self.db.get("session", ""),
                        text,
                        "pending",
                        rev,
                        int(proactive),
                        iso(now),
                        "",
                        dump(sorted(set(result.used_memory_ids) & available)),
                    ),
                )
            await self.flush()
        if result.next_plan and rev == self.db.get("revision"):
            due = parse(result.next_plan.at)
            if due <= now:
                raise ValueError("下一次计划必须位于未来")
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE kind='proactive' AND status='pending'")
            plan = result.next_plan.model_dump()
            plan["memory_ids"] = list(set(plan["memory_ids"]) & available)
            self.db.add_job("proactive", due, {"plan": plan}, rev)

    async def flush(self):
        for msg in self.db.rows("SELECT * FROM outbox WHERE status='pending' ORDER BY rowid"):
            async with self.gate:
                if msg["revision"] != self.db.get("revision") or (msg["proactive"] and self.db.get("paused")):
                    self.db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (msg["id"],))
                    continue
                self.db.execute(
                    "UPDATE outbox SET status='sending' WHERE id=? AND status='pending'", (msg["id"],)
                )
                result = await self.transport.send(msg["session"], msg["content"])
                self.db.execute(
                    "UPDATE outbox SET status=?,detail=? WHERE id=?",
                    (result.status, result.detail, msg["id"]),
                )
                if result.status == "sent":
                    now = self.clock()
                    self.db.execute(
                        "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,1,?)",
                        (
                            msg["id"],
                            msg["session"],
                            "assistant",
                            msg["content"],
                            iso(now),
                            msg["revision"],
                            "out:" + msg["id"],
                        ),
                    )
                    self.memory.reinforce(json.loads(msg["used_ids"]), now)
                else:
                    self.db.audit("send." + result.status, {"id": msg["id"], "detail": result.detail})
                    # Don't send later fragments after a missing/uncertain fragment.
                    self.db.execute(
                        "UPDATE outbox SET status='cancelled' WHERE status='pending' AND revision=?",
                        (msg["revision"],),
                    )
                    break
            pending_fragment = self.db.one(
                "SELECT id FROM outbox WHERE status='pending' AND revision=? LIMIT 1",
                (msg["revision"],),
            )
            if pending_fragment and self.settings.fragment_delay_seconds:
                # Release the gate during the pause so a new user message can
                # cancel the remaining fragments.
                await asyncio.sleep(self.settings.fragment_delay_seconds)
            else:
                await asyncio.sleep(0)  # Let a newly arrived turn preempt work.

    async def life(self):
        now = self.clock()
        local = now.astimezone(SHANGHAI)
        day = local.date().isoformat()
        persona = self.db.persona()
        if self.db.get("schedule_day") != day:
            self.db.execute("UPDATE events SET status='cancelled' WHERE status='planned' AND day!=?", (day,))
            ctx = await self.context("今天的课程和生活安排", "schedule")
            ctx["date"] = day
            version = persona["version"]
            rev = self.db.get("revision")
            schedule = await self.model.generate("schedule", ctx, DaySchedule)
            if version != self.db.persona()["version"] or rev != self.db.get("revision"):
                return
            last_end = now
            for activity in sorted(schedule.activities, key=lambda a: parse(a.at)):
                start, end = parse(activity.at), parse(activity.until)
                if (
                    start < now
                    or end <= start
                    or start < last_end
                    or start.astimezone(SHANGHAI).date() != local.date()
                ):
                    continue
                self.db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,NULL,'planned',?,?)",
                    (uid(), iso(start), iso(end), activity.title, activity.outline, version, day),
                )
                last_end = end
            self.db.set("schedule_day", day)
        events = self.db.rows(
            "SELECT * FROM events WHERE status='planned' AND at<=? ORDER BY at", (iso(now),)
        )
        # On wake, resolve missed activities as life history, never as queued outgoing messages.
        for event in events:
            ctx = {
                "now": iso(now),
                "persona": persona,
                "activity": event,
                "state": self.db.get("state"),
                "instruction": "若活动已结束，回顾已发生部分，不产生对外消息。",
            }
            result = await self.model.generate("experience", ctx, Experience)
            if self.db.persona()["version"] != event["persona_version"]:
                self.db.execute("UPDATE events SET status='cancelled' WHERE id=?", (event["id"],))
                continue
            self.db.execute(
                "UPDATE events SET status='occurred',content=? WHERE id=?", (result.content, event["id"])
            )
            self.db.set("state", result.state[:300])
            for draft in result.memories:
                if draft.source_type == "virtual":
                    await self.memory.add(draft, {event["id"]}, now)
            if self.db.one("SELECT id FROM jobs WHERE status='pending' AND kind='turn'"):
                return
        # Finalize the previous active day after downtime, and today's diary at 23:30.
        days = {
            r["day"]
            for r in self.db.rows("SELECT DISTINCT day FROM events WHERE status='occurred' AND day<?", (day,))
        }
        days.update(r["day"] for r in self.db.rows("SELECT DISTINCT day FROM diaries WHERE day<?", (day,)))
        if (local.hour, local.minute) >= (23, 30):
            days.add(day)
        for target in sorted(days):
            await self.write_diary(target, now)
        last_maintenance = self.db.get("maintenance_day")
        if last_maintenance != day and (
            (local.hour, local.minute) >= (23, 30)
            or last_maintenance is None
            or last_maintenance < (local.date() - timedelta(days=1)).isoformat()
        ):
            await self.memory.maintain(self.model, now)
            self.db.set("maintenance_day", day)

    async def write_diary(self, day, now):
        start = datetime.fromisoformat(day).replace(tzinfo=SHANGHAI)
        end = start + timedelta(days=1)
        events = [
            e
            for e in self.db.rows(
                "SELECT id,at,title,content FROM events WHERE day=? AND status='occurred'", (day,)
            )
            if self.memory.safe_sources([e["id"]])
        ]
        messages = [
            m
            for m in self.db.rows(
                "SELECT id,at,role,content FROM messages WHERE visible=1 AND at>=? AND at<?",
                (iso(start), iso(end)),
            )
            if self.memory.safe_sources([m["id"]])
        ]
        sources = {r["id"] for r in events + messages}
        old = self.db.rows("SELECT * FROM diaries WHERE day=?", (day,))
        covered = {i for d in old for i in json.loads(d["source_ids"])}
        unseen = sources - covered
        if not unseen:
            return
        # Only new source material is included in an addendum. Old archive text is never reloaded.
        result = await self.model.generate(
            "diary",
            {
                "now": iso(now),
                "date": day,
                "is_addendum": bool(old),
                "events": [e for e in events if e["id"] in unseen],
                "messages": [m for m in messages if m["id"] in unseen],
            },
            Diary,
        )
        self.db.execute(
            "INSERT INTO diaries VALUES (?,?,?,?,?,1)",
            (uid(), day, iso(now), result.content, dump(sorted(unseen))),
        )

    async def tick(self):
        if self.tick_lock.locked():
            return
        async with self.tick_lock:
            now = self.clock()
            if self.cooldown and now < self.cooldown:
                return
            try:
                await self.initialize()
                if not self.db.persona():
                    waiting = "还没有角色设定。在「角色设定」页写下第一段描述，生成之后角色就会开始今天的生活。"
                    if self.db.get("state") != waiting:
                        self.db.set("state", waiting)
                    return
                job = self.db.one(
                    """SELECT * FROM jobs WHERE status='pending' AND due<=?
                    AND (kind!='proactive' OR ?=0)
                    ORDER BY CASE kind WHEN 'turn' THEN 0 WHEN 'reply' THEN 1 ELSE 2 END,due LIMIT 1""",
                    (iso(now), int(self.db.get("paused"))),
                )
                if job:
                    self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
                    try:
                        await self.decide(job)
                    except Exception:
                        payload = json.loads(job["payload"])
                        attempts = payload.get("attempts", 0) + 1
                        payload["attempts"] = attempts
                        effects = self.db.one("SELECT id FROM outbox WHERE revision=?", (job["revision"],))
                        if attempts < 3 and not effects and job["revision"] == self.db.get("revision"):
                            self.db.execute(
                                "UPDATE jobs SET status='pending',payload=?,due=? WHERE id=?",
                                (dump(payload), iso(now + timedelta(seconds=30 * attempts)), job["id"]),
                            )
                        else:
                            self.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
                        raise
                    self.db.execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],))
                else:
                    await self.life()
                self.model_failures = 0
                self.db.set("last_error", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.model_failures += 1
                self.cooldown = now + timedelta(seconds=min(300, 10 * 2 ** min(self.model_failures, 5)))
                error = {"type": type(exc).__name__, "at": iso(now), "retry_after": iso(self.cooldown)}
                self.db.set("last_error", error)
                self.db.audit("runtime.error", error)

    async def run(self):
        self.db.recover()
        # Pending fragments from before restart are re-evaluated rather than replayed.
        pending = self.db.rows("SELECT * FROM outbox WHERE status='pending'")
        if pending:
            self.db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending'")
            self.db.add_job("reply", self.clock(), {"recovery": True})
        while not self.stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
