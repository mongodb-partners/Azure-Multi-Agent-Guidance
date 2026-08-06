"""Atlas Search index definitions + idempotent creation.

Each semantically-searched collection gets:
  - a Vector Search index  (`vector_index`)  on `embedding`  — matches retrieval.py defaults.
  - an Atlas Search index   (`search_index`)  (BM25, dynamic) for lexical/hybrid search.

Dimensions come from EMBEDDING_DIMENSIONS (3072 for text-embedding-3-large), cosine similarity.
Requires an Atlas tier that supports Vector Search (M10+ or Flex) and pymongo >= 4.7.
"""
from __future__ import annotations

import logging

from src.settings import get_settings

logger = logging.getLogger(__name__)

# Collections that carry an `embedding` field and need hybrid search.
VECTOR_COLLECTIONS = ["jobs", "companies", "interview_prep", "candidates"]
VECTOR_INDEX_NAME = "vector_index"
SEARCH_INDEX_NAME = "search_index"


def _vector_definition(dims: int) -> dict:
    return {
        "fields": [
            {"type": "vector", "path": "embedding", "numDimensions": dims, "similarity": "cosine"}
        ]
    }


# Dynamic BM25 mapping keeps seeding simple; retrieval.py targets specific text paths at query time.
_SEARCH_DEFINITION = {"mappings": {"dynamic": True}}


def _existing_index_names(collection) -> set[str]:
    try:
        return {ix["name"] for ix in collection.list_search_indexes()}
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not list search indexes on %s: %s", collection.name, e)
        return set()


def create_indexes(db) -> None:
    """Create vector + BM25 indexes on all vector collections (idempotent)."""
    from pymongo.operations import SearchIndexModel

    dims = get_settings().embedding_dimensions
    for coll_name in VECTOR_COLLECTIONS:
        coll = db[coll_name]
        existing = _existing_index_names(coll)

        if VECTOR_INDEX_NAME not in existing:
            coll.create_search_index(
                SearchIndexModel(
                    definition=_vector_definition(dims),
                    name=VECTOR_INDEX_NAME,
                    type="vectorSearch",
                )
            )
            logger.info("Created vector index on %s (%d dims)", coll_name, dims)
        else:
            logger.info("Vector index already exists on %s", coll_name)

        if SEARCH_INDEX_NAME not in existing:
            coll.create_search_index(
                SearchIndexModel(definition=_SEARCH_DEFINITION, name=SEARCH_INDEX_NAME)
            )
            logger.info("Created BM25 search index on %s", coll_name)
        else:
            logger.info("BM25 search index already exists on %s", coll_name)

    print(
        "\nAtlas index builds submitted. They build asynchronously — check the Atlas UI "
        "(Search tab) or `db.<coll>.getSearchIndexes()` until status is READY before querying."
    )
