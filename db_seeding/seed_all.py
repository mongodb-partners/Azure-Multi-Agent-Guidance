"""Seed the Career Assistant dataset into Atlas (FabricCluster / multi-agent).

Steps:
  1. Connect to MongoDB Atlas via MONGODB_URI.
  2. Upsert sample documents into each collection (idempotent by natural id).
  3. Per EMBEDDING_PROVIDER (specs/002):
     - foundry/voyage  → generate `embedding` vectors via embeddings.embed_text.
     - atlas_auto      → write raw text into `embed_text` and strip any stale
                         `embedding` field; Atlas generates vectors itself (R3).
  4. Create Atlas Vector Search + BM25 indexes (indexes.py).

Run from the project root:  `python -m db_seeding.seed_all`
Flags: `--no-index` skip index creation, `--drop` clear collections first,
`--recreate-indexes` drop and rebuild the vector index (required after a provider
switch changes dims/type — indexes.py fails fast on a mismatch otherwise, R5).
"""
from __future__ import annotations

import logging
import sys

from pymongo import MongoClient

from src.embeddings import AUTO_EMBED_TEXT_FIELD, embed_text
from src.settings import get_settings

from .indexes import VECTOR_COLLECTIONS, create_indexes
from .sample_data import DATASET

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed")

# Natural key per collection for idempotent upserts.
_NATURAL_KEY = {
    "candidates": "candidate_id",
    "jobs": "job_id",
    "companies": "company_id",
    "interview_prep": None,  # composite → replace-all
    "career_paths": None,
    "skills_taxonomy": "skill",
    "compensation_data": None,
}


def seed(drop: bool = False, build_indexes: bool = True, recreate_indexes: bool = False) -> None:
    s = get_settings()
    s.require("mongodb_uri")
    provider = s.embedding_provider
    logger.info("Connected to DB '%s' (embedding provider: %s)", s.mongodb_db, provider)
    client = MongoClient(s.mongodb_uri)
    db = client[s.mongodb_db]

    for coll_name, docs, text_builder in DATASET:
        coll = db[coll_name]
        if drop:
            coll.delete_many({})
            logger.info("Cleared %s", coll_name)

        key = _NATURAL_KEY.get(coll_name)
        needs_embedding = coll_name in VECTOR_COLLECTIONS and text_builder is not None

        for doc in docs:
            record = dict(doc)
            unset: dict[str, str] = {}
            if needs_embedding:
                if provider == "atlas_auto":
                    # Atlas embeds this field itself; drop any stale client-computed vector.
                    record[AUTO_EMBED_TEXT_FIELD] = text_builder(doc)
                    unset["embedding"] = ""
                else:
                    record["embedding"] = embed_text(text_builder(doc), input_type="document")

            if key and key in record:
                match = {key: record[key]}
            else:
                # No natural key: avoid dupes by matching the whole doc shape on a stable field.
                match = {"question": record["question"]} if coll_name == "interview_prep" else record
            update: dict = {"$set": record}
            if unset:
                update["$unset"] = unset
            coll.update_one(match, update, upsert=True)

        logger.info("Seeded %s (%d docs, embeddings=%s)", coll_name, len(docs),
                    "atlas-managed" if (needs_embedding and provider == "atlas_auto")
                    else needs_embedding)

    if build_indexes:
        create_indexes(db, recreate=recreate_indexes)

    logger.info("Seeding complete.")


if __name__ == "__main__":
    seed(
        drop="--drop" in sys.argv,
        build_indexes="--no-index" not in sys.argv,
        recreate_indexes="--recreate-indexes" in sys.argv,
    )
