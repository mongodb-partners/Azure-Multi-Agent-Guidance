"""Atlas Search index definitions + idempotent creation (provider-aware, specs/002).

Each semantically-searched collection gets:
  - a Vector Search index (`vector_index`):
      * foundry/voyage  → `vector` field on `embedding`, dims from the active provider
                          (embeddings.embed_dimensions()), cosine similarity.
      * atlas_auto      → `autoEmbed` field on the raw-text `embed_text` field declaring
                          the Atlas-managed Voyage model (ATLAS_EMBED_MODEL) — Atlas
                          generates and maintains embeddings itself (R3).
  - an Atlas Search index  (`search_index`)  (BM25, dynamic) for lexical/hybrid search.

R5 guard: if an existing `vector_index` doesn't match the active provider's definition
(type/path/dims/model), creation fails fast with instructions — re-run with
`python -m db_seeding.seed_all --recreate-indexes` to drop and rebuild.

Requires an Atlas tier that supports Vector Search (M10+ or Flex) and pymongo >= 4.7.
"""
from __future__ import annotations

import logging

from src.embeddings import AUTO_EMBED_TEXT_FIELD, embed_dimensions
from src.settings import get_settings

logger = logging.getLogger(__name__)

# Collections that carry semantic-search content and need hybrid search.
VECTOR_COLLECTIONS = ["jobs", "companies", "interview_prep", "candidates"]
VECTOR_INDEX_NAME = "vector_index"
SEARCH_INDEX_NAME = "search_index"


def _vector_definition(dims: int) -> dict:
    return {
        "fields": [
            {"type": "vector", "path": "embedding", "numDimensions": dims, "similarity": "cosine"}
        ]
    }


def _auto_embed_definition(model: str) -> dict:
    return {
        "fields": [
            {"type": "autoEmbed", "modality": "text", "path": AUTO_EMBED_TEXT_FIELD,
             "model": model}
        ]
    }


# Dynamic BM25 mapping keeps seeding simple; retrieval.py targets specific text paths at query time.
_SEARCH_DEFINITION = {"mappings": {"dynamic": True}}


def desired_vector_definition() -> dict:
    """The vector-index definition for the active embedding provider."""
    s = get_settings()
    if s.embedding_provider == "atlas_auto":
        return _auto_embed_definition(s.atlas_embed_model)
    dims = embed_dimensions()
    if not dims:
        raise RuntimeError(
            f"Could not determine embedding dimensions for provider "
            f"{s.embedding_provider!r} — set EMBEDDING_DIMENSIONS / VOYAGE_DIMENSIONS."
        )
    return _vector_definition(dims)


def _index_signature(definition: dict) -> list[tuple]:
    """Comparable essence of a vector index def: (type, path, numDimensions, model)."""
    return sorted(
        (
            f.get("type"),
            f.get("path"),
            f.get("numDimensions"),
            f.get("model"),
        )
        for f in definition.get("fields", [])
    )


def _check_compatible(collection, desired: dict, recreate: bool) -> bool:
    """True if the existing vector_index may be kept; drop/raise otherwise (R5)."""
    try:
        existing = [
            ix for ix in collection.list_search_indexes() if ix["name"] == VECTOR_INDEX_NAME
        ]
    except Exception as e:  # noqa: BLE001 — e.g. collection not materialized yet
        logger.warning("Could not inspect search indexes on %s: %s", collection.name, e)
        return False
    if not existing:
        return False
    current = existing[0].get("latestDefinition", {})
    if _index_signature(current) == _index_signature(desired):
        return True
    if recreate:
        logger.warning("Dropping mismatched vector index on %s (--recreate-indexes)",
                       collection.name)
        collection.drop_search_index(VECTOR_INDEX_NAME)
        return False
    raise RuntimeError(
        f"vector_index on '{collection.name}' does not match the active embedding "
        f"provider (existing: {_index_signature(current)}, desired: "
        f"{_index_signature(desired)}). Mixed-dimension vectors silently break search. "
        "Re-run with `python -m db_seeding.seed_all --drop --recreate-indexes` to rebuild "
        "data and indexes for the new provider."
    )


def _existing_index_names(collection) -> set[str]:
    try:
        return {ix["name"] for ix in collection.list_search_indexes()}
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not list search indexes on %s: %s", collection.name, e)
        return set()


def create_indexes(db, recreate: bool = False) -> None:
    """Create vector + BM25 indexes on all vector collections (idempotent)."""
    from pymongo.operations import SearchIndexModel

    s = get_settings()
    desired = desired_vector_definition()
    for coll_name in VECTOR_COLLECTIONS:
        coll = db[coll_name]

        if not _check_compatible(coll, desired, recreate):
            coll.create_search_index(
                SearchIndexModel(
                    definition=desired,
                    name=VECTOR_INDEX_NAME,
                    type="vectorSearch",
                )
            )
            logger.info("Created vector index on %s (provider=%s, def=%s)",
                        coll_name, s.embedding_provider, _index_signature(desired))
        else:
            logger.info("Vector index already exists on %s (compatible)", coll_name)

        if SEARCH_INDEX_NAME not in _existing_index_names(coll):
            coll.create_search_index(
                SearchIndexModel(definition=_SEARCH_DEFINITION, name=SEARCH_INDEX_NAME)
            )
            logger.info("Created BM25 search index on %s", coll_name)
        else:
            logger.info("BM25 search index already exists on %s", coll_name)

    extra = (
        " Atlas generates embeddings asynchronously (initial sync) — the index is not "
        "queryable until status is READY." if s.embedding_provider == "atlas_auto" else ""
    )
    print(
        "\nAtlas index builds submitted. They build asynchronously — check the Atlas UI "
        "(Search tab) or `db.<coll>.getSearchIndexes()` until status is READY before querying."
        + extra
    )
