import asyncio
import hashlib
import json
import math
import re
from datetime import timedelta

from .domain import CompressedMemory, iso, parse, uid, utcnow
from .store import dump


def tokens(text):
    parts = set(re.findall(r"[a-z0-9]+", text.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        parts.update(chunk[i : i + 2] for i in range(max(1, len(chunk) - 1)))
    return parts


class LocalEmbedder:
    def __init__(self, settings):
        self.settings = settings
        self.model = None
        self.lock = asyncio.Lock()

    async def encode(self, texts):
        async with self.lock:
            return await asyncio.to_thread(self._encode, texts)

    def _encode(self, texts):
        if self.model is None:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(
                self.settings.embedding_model,
                device="cpu",
                local_files_only=self.settings.embedding_local_only,
                trust_remote_code=False,
            )
        return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


class Memory:
    def __init__(self, store, settings, embedder):
        self.store, self.settings, self.embedder = store, settings, embedder

    def available(self):
        return self.store.rows("SELECT * FROM memories WHERE status='active'")

    def safe_sources(self, source_ids):
        blocked = set()
        for m in self.store.rows("SELECT source_ids FROM memories WHERE status!='active'"):
            blocked.update(json.loads(m["source_ids"]))
        return not (set(source_ids) & blocked)

    async def add(self, draft, allowed_sources, now=None):
        now = now or utcnow()
        if not draft.source_ids or not set(draft.source_ids) <= set(allowed_sources):
            return None
        if parse(draft.occurred_at) > now:
            return None
        if draft.source_type == "user" and draft.kind == "inference":
            return None
        # Sources themselves determine provenance, never the model's label alone.
        if draft.source_type == "user":
            for source in draft.source_ids:
                msg = self.store.one("SELECT role FROM messages WHERE id=?", (source,))
                if not msg or msg["role"] != "user":
                    return None
        if draft.source_type == "virtual":
            if any(
                not self.store.one("SELECT id FROM events WHERE id=? AND status='occurred'", (i,))
                for i in draft.source_ids
            ):
                return None
        identity = hashlib.sha256(
            (draft.content.strip() + dump(sorted(draft.source_ids))).encode()
        ).hexdigest()
        if self.store.one("SELECT id FROM memories WHERE id=?", (identity,)):
            return identity
        replaced = None
        if draft.supersedes:
            replaced = self.store.one(
                "SELECT * FROM memories WHERE id=? AND status='active'", (draft.supersedes,)
            )
            if not replaced or not draft.explicit_correction:
                return None
            if replaced["source_type"] == "user" and draft.source_type != "user":
                return None
        vector = (await self.embedder.encode([draft.content]))[0]
        with self.store.connect() as c:
            c.execute(
                """INSERT INTO memories(id,content,subject,kind,source_type,source_ids,occurred_at,
             created,entities,topics,importance,confidence,unresolved,last_used,supersedes,vector)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity,
                    draft.content,
                    draft.subject,
                    draft.kind,
                    draft.source_type,
                    dump(draft.source_ids),
                    iso(parse(draft.occurred_at)),
                    iso(now),
                    dump(draft.entities),
                    dump(draft.topics),
                    draft.importance,
                    draft.confidence,
                    int(draft.unresolved),
                    iso(now),
                    draft.supersedes,
                    dump(vector),
                ),
            )
            if replaced:
                c.execute("UPDATE memories SET status='superseded',vector=NULL WHERE id=?", (replaced["id"],))
            for link in draft.links:
                if c.execute(
                    "SELECT id FROM memories WHERE id=? AND status='active'", (link.target,)
                ).fetchone():
                    c.execute(
                        "INSERT OR IGNORE INTO links VALUES (?,?,?,?)",
                        (identity, link.target, link.kind, link.strength),
                    )
            if replaced:
                c.execute(
                    "INSERT OR IGNORE INTO links VALUES (?,?,?,1)", (identity, replaced["id"], "correction")
                )
        if replaced:
            self.invalidate_sources(replaced)
        return identity

    async def search(self, query, now=None, record=True):
        now = now or utcnow()
        memories = self.available()
        if not memories:
            return []
        missing = [m for m in memories if not m["vector"]]
        if missing:
            vectors = await self.embedder.encode([m["content"] for m in missing])
            for m, v in zip(missing, vectors):
                m["vector"] = dump(v)
                self.store.execute("UPDATE memories SET vector=? WHERE id=?", (m["vector"], m["id"]))
        qvec = (await self.embedder.encode([query]))[0]
        qt = tokens(query)
        scores = {}
        for m in memories:
            semantic = max(0, min(1, sum(a * b for a, b in zip(qvec, json.loads(m["vector"])))))
            mt = tokens(m["content"] + " " + " ".join(json.loads(m["entities"])))
            keyword = len(qt & mt) / max(1, len(qt))
            age = max(0, (now - parse(m["last_used"])).total_seconds() / 86400)
            half = 180 if m["kind"] in ("fact", "preference") else 14
            scores[m["id"]] = {
                "semantic": semantic,
                "keyword": keyword,
                "connection": 0.0,
                "time": math.exp(-math.log(2) * age / half),
                "paths": [],
            }
        # Similarity is a relative signal; exclude very weak matches before time contributes.
        sem_seeds = sorted(scores, key=lambda i: scores[i]["semantic"], reverse=True)[:30]
        key_seeds = sorted(scores, key=lambda i: scores[i]["keyword"], reverse=True)[:30]
        seeds = {i for i in sem_seeds if scores[i]["semantic"] >= 0.60}
        seeds.update(i for i in key_seeds if scores[i]["keyword"] > 0)
        candidates = set(seeds)
        for link in self.store.rows("SELECT * FROM links"):
            for source, target in [(link["source"], link["target"]), (link["target"], link["source"])]:
                if source in seeds and target in scores:
                    candidates.add(target)
                    value = link["strength"] * max(scores[source]["semantic"], scores[source]["keyword"])
                    scores[target]["connection"] = max(scores[target]["connection"], value)
                    scores[target]["paths"].append({"from": source, "kind": link["kind"]})
        by_id = {m["id"]: m for m in memories}
        total_weight = sum(self.settings.weights.values())
        ranked = []
        for i in candidates:
            parts = scores[i]
            score = sum(self.settings.weights[k] * parts[k] for k in self.settings.weights) / total_weight
            m = by_id[i]
            ranked.append(
                {
                    "id": i,
                    "content": m["content"],
                    "source_type": m["source_type"],
                    "kind": m["kind"],
                    "source_ids": json.loads(m["source_ids"]),
                    "occurred_at": m["occurred_at"],
                    "score": score,
                    "scores": parts,
                }
            )
        ranked.sort(key=lambda m: (-m["score"], m["id"]))
        results, size = [], 0
        seen = set()
        for m in ranked[:80]:
            if m["content"] in seen:
                continue
            cost = len(dump(m))
            if size + cost > self.settings.memory_chars:
                continue
            results.append(m)
            seen.add(m["content"])
            size += cost
            if len(results) == 12:
                break
        if record:
            self.store.execute(
                "INSERT INTO retrievals VALUES (?,?,?,?)", (uid(), iso(now), query, dump(results))
            )
        return results

    def reinforce(self, ids, now):
        for i in set(ids):
            self.store.execute(
                "UPDATE memories SET last_used=? WHERE id=? AND status='active'", (iso(now), i)
            )

    def invalidate_sources(self, memory):
        sources = json.loads(memory["source_ids"])
        for source in sources:
            self.store.execute("UPDATE messages SET visible=0 WHERE id=?", (source,))
        # Derived assistant statements, summaries and pending plans cannot leak forgotten details.
        for out in self.store.rows("SELECT id,used_ids FROM outbox"):
            if memory["id"] in json.loads(out["used_ids"]):
                self.store.execute("UPDATE messages SET visible=0 WHERE id=?", (out["id"],))
        for diary in self.store.rows("SELECT id,source_ids FROM diaries WHERE visible=1"):
            if set(sources) & set(json.loads(diary["source_ids"])):
                self.store.execute("UPDATE diaries SET visible=0 WHERE id=?", (diary["id"],))
        for job in self.store.rows("SELECT id,payload FROM jobs WHERE status='pending'"):
            if memory["id"] in job["payload"]:
                self.store.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job["id"],))

    def retire(self, memory, status):
        self.store.execute("UPDATE memories SET status=?,vector=NULL WHERE id=?", (status, memory["id"]))
        self.invalidate_sources(memory)
        self.store.set("state", "正在继续今天的生活")
        self.store.audit("memory." + status, {"id": memory["id"]})

    def delete(self, memory):
        """Delete directly sourced material and its derived snapshots, leaving content-free tombstones."""
        sources = set(json.loads(memory["source_ids"]))
        related = [
            m for m in self.store.rows("SELECT * FROM memories") if sources & set(json.loads(m["source_ids"]))
        ]
        ids = {m["id"] for m in related}
        for m in related:
            self.retire(m, "deleted")
            self.store.execute(
                "UPDATE memories SET content='[已删除]',subject='',entities='[]',topics='[]' WHERE id=?",
                (m["id"],),
            )
            self.store.execute("DELETE FROM links WHERE source=? OR target=?", (m["id"], m["id"]))
        for source in sources:
            self.store.execute("UPDATE messages SET content='[已删除]',visible=0 WHERE id=?", (source,))
            self.store.execute(
                "UPDATE events SET content='[已删除]',title='[已删除]',outline='' WHERE id=?", (source,)
            )
        for row in self.store.rows("SELECT * FROM diaries"):
            if sources & set(json.loads(row["source_ids"])):
                self.store.execute(
                    "UPDATE diaries SET content='[关联原文已删除]',visible=0 WHERE id=?", (row["id"],)
                )
        for row in self.store.rows("SELECT * FROM outbox"):
            if ids & set(json.loads(row["used_ids"])):
                self.store.execute("UPDATE outbox SET content='[关联记忆已删除]' WHERE id=?", (row["id"],))
                self.store.execute(
                    "UPDATE messages SET content='[关联记忆已删除]',visible=0 WHERE id=?", (row["id"],)
                )
        for row in self.store.rows("SELECT * FROM retrievals"):
            results = json.loads(row["results"])
            if any(m["id"] in ids for m in results):
                self.store.execute(
                    "UPDATE retrievals SET query='[关联记忆已删除]',results=? WHERE id=?",
                    (dump([m for m in results if m["id"] not in ids]), row["id"]),
                )
        for row in self.store.rows("SELECT * FROM jobs"):
            if any(i in row["payload"] for i in ids | sources):
                self.store.execute("UPDATE jobs SET payload='{}',status='cancelled' WHERE id=?", (row["id"],))

    async def maintain(self, model, now=None):
        now = now or utcnow()
        for m in self.available():
            if m["pinned"] or m["unresolved"] or m["importance"] >= self.settings.low_importance:
                continue
            age = now - parse(m["last_used"])
            if age >= timedelta(days=self.settings.forget_days):
                self.retire(m, "forgotten")
            elif age >= timedelta(days=self.settings.compress_days) and not m["summary_of"]:
                summary = await model.generate("compress", {"content": m["content"]}, CompressedMemory)
                vector = (await self.embedder.encode([summary.content]))[0]
                new_id = uid()
                with self.store.connect() as c:
                    c.execute(
                        """INSERT INTO memories(id,content,subject,kind,source_type,source_ids,occurred_at,
                     created,entities,topics,importance,confidence,last_used,vector,summary_of)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            new_id,
                            summary.content,
                            m["subject"],
                            m["kind"],
                            m["source_type"],
                            m["source_ids"],
                            m["occurred_at"],
                            iso(now),
                            "[]",
                            "[]",
                            m["importance"],
                            m["confidence"],
                            m["last_used"],
                            dump(vector),
                            m["id"],
                        ),
                    )
                self.retire(m, "compressed")
