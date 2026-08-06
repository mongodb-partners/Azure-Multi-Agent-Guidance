"""Seed the Career Assistant dataset into Atlas (FabricCluster / multi-agent).

Steps:
  1. Connect to MongoDB Atlas via MONGODB_URI.
  2. Upsert sample documents into each collection (idempotent by natural id).
  3. Generate `embedding` vectors for semantically-searched collections via embeddings.embed_text.
  4. Create Atlas Vector Search + BM25 indexes (indexes.py).

Run from the project root:  `python -m db_seeding.seed_all`
Add `--no-index` to skip index creation, `--drop` to clear collections first.
"""
from __future__ import annotations

import logging
import sys

from pymongo import MongoClient

from src.embeddings import embed_text
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


def seed(drop: bool = False, build_indexes: bool = True) -> None:
    s = get_settings()
    s.require("mongodb_uri")
    client = MongoClient(s.mongodb_uri)
    db = client[s.mongodb_db]
    logger.info("Connected to DB '%s'", s.mongodb_db)

    for coll_name, docs, text_builder in DATASET:
        coll = db[coll_name]
        if drop:
            coll.delete_many({})
            logger.info("Cleared %s", coll_name)

        key = _NATURAL_KEY.get(coll_name)
        needs_embedding = coll_name in VECTOR_COLLECTIONS and text_builder is not None

        for doc in docs:
            record = dict(doc)
            if needs_embedding:
                record["embedding"] = embed_text(text_builder(doc))

            if key and key in record:
                coll.update_one({key: record[key]}, {"$set": record}, upsert=True)
            else:
                # No natural key: avoid dupes by matching the whole doc shape on a stable field.
                match = {"question": record["question"]} if coll_name == "interview_prep" else record
                coll.update_one(match, {"$set": record}, upsert=True)

        logger.info("Seeded %s (%d docs, embeddings=%s)", coll_name, len(docs), needs_embedding)

    if build_indexes:
        create_indexes(db)

    logger.info("Seeding complete.")


if __name__ == "__main__":
    seed(drop="--drop" in sys.argv, build_indexes="--no-index" not in sys.argv)
