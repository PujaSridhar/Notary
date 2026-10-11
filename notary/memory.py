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


def store() -> AtlasVoyageStore:
    return AtlasVoyageStore()
