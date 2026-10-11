"""MemoryStore seam (SPEC section 5a). Trusted lessons live here; only the gate writes."""

from __future__ import annotations

import json
import math
import os
import urllib.request

from .db import db, now

EMBED_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-4-lite")
DUPLICATE_THRESHOLD = float(os.environ.get("NOTARY_DUP_THRESHOLD", "0.92"))


def embed(texts: list[str]) -> list[list[float]]:
    base = os.environ.get("VOYAGE_BASE_URL", "https://ai.mongodb.com/v1")
    req = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps({"input": texts, "model": EMBED_MODEL}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)["data"]
    return [row["embedding"] for row in sorted(data, key=lambda x: x["index"])]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


class AtlasVoyageStore:
    """Fallback store: a `lessons` collection in the Atlas Sandbox with Voyage embeddings."""

    def write_lesson(self, candidate: dict) -> str:
        vec = embed([candidate["text"]])[0]
        db().lessons.update_one(
            {"_id": candidate["_id"]},
            {"$set": {"text": candidate["text"], "embedding": vec, "embed_model": EMBED_MODEL,
                      "source_trust": candidate["source_trust"], "written_at": now()}},
            upsert=True)
        return str(candidate["_id"])

    def nearest_active(self, text: str, active_ids: list) -> tuple[float, dict | None]:
        """Pre-gate duplicate check: similarity of a candidate to the closest active lesson."""
        active = list(db().lessons.find({"_id": {"$in": active_ids}}))
        if not active:
            return 0.0, None
        vec = embed([text])[0]
        best = max(active, key=lambda l: cosine(vec, l["embedding"]))
        return cosine(vec, best["embedding"]), best


AGENT_USER = "acme-refund-agent"  # one agent identity; all lessons live in its procedural memory


class AgentEngineStore(AtlasVoyageStore):
    """Primary store: Atlas Agent Engine procedural memory. Notary is the only writer (background
    extraction of procedural memory is disabled in agentengine/project-config.yaml). Lessons are also
    mirrored in the Atlas `lessons` collection for the Voyage duplicate check and as a fallback."""

    def _mem(self):
        from agent_engine_sdk_memory import Memory, MemoryRequestContext

        from .ae_auth import access_token
        return Memory(service_account_token=access_token()).bind(MemoryRequestContext(user_id=AGENT_USER))

    def write_lesson(self, candidate: dict) -> str:
        atlas_ref = super().write_lesson(candidate)  # Atlas mirror + embedding (always)
        try:
            return self._write_agent_engine(candidate)
        except Exception as e:  # Agent Engine is in public preview; never let it break promotion
            db().lessons.update_one({"_id": candidate["_id"]},
                                    {"$set": {"agent_engine_error": repr(e)[:300]}})
            return f"atlas:{atlas_ref} (agent engine unavailable)"

    def _write_agent_engine(self, candidate: dict) -> str:
        res = self._mem().save_procedure(
            procedure=f"notary-{candidate['_id']}",
            description="Refund-desk lesson admitted by the Notary evaluation gate",
            content=candidate["text"],
            tags=["notary", "refund-desk"],
            extraction_source="notary-gate",
            metadata={"notary_candidate_id": str(candidate["_id"]),
                      "source_trust": candidate["source_trust"], "admitted_by": "notary-gate"})
        db().lessons.update_one({"_id": candidate["_id"]}, {"$set": {"agent_engine_result": str(res)[:500]}})
        return f"agent-engine:notary-{candidate['_id']}"

    def write_ungated(self, text: str, tag: str) -> str:
        """The naive path, for the comparison demo only: what unchecked extraction would store."""
        res = self._mem().save_procedure(
            procedure=f"ungated-{tag}", description="UNGATED write (comparison demo)", content=text,
            tags=["ungated"], extraction_source="ungated-demo", metadata={"admitted_by": "nobody"})
        return str(res)[:200]

    def lessons_for(self, query: str, active_ids: list) -> list[str]:
        """Agent read path: discover procedures in Agent Engine, then serve only those in the active version."""
        wanted = {str(i) for i in active_ids}
        hits = self._mem().discover_procedures(query, top_k=50, tags=["notary"])
        out = []
        for h in hits:
            meta = h.get("metadata") or {}
            if meta.get("notary_candidate_id") in wanted:
                out.append(h.get("content") or h.get("text") or "")
        return [t for t in out if t]


def agent_engine_enabled() -> bool:
    return bool(os.environ.get("AE_CLIENT_ID")) and os.environ.get("NOTARY_MEMORY", "agent_engine") == "agent_engine"


def store() -> AtlasVoyageStore:
    return AgentEngineStore() if agent_engine_enabled() else AtlasVoyageStore()
