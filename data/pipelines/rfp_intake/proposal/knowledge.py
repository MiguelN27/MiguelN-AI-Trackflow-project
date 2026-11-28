"""The generators' access to the company knowledge base (the ticket's optional item).

Drafting with TrackFlow's real policies and wording, instead of improvising
them, makes a section much more likely to pass compliance on the first try.

- First the semantic knowledge base (Milestone 7): `rag.retrieve` in Qdrant,
  one query per subject the department needs (`sections.KB_QUERIES`).
- When Qdrant or the embeddings model is unavailable, or nothing scores above
  the threshold, the same four documents are read from
  `docs/company-knowledge-base/` (the indexed corpus, verbatim; about 150 words
  each). Drafting never stops for want of the vector database.

The source used (`qdrant`, `local` or `none`) is recorded with the draft.
"""

import logging
from pathlib import Path
from typing import Any, Final, Literal

from data.pipelines.rfp_intake.config import ROOT_DIR
from data.process.knowledge_base import KnowledgeBaseError

logger = logging.getLogger("pipelines.rfp_intake.proposal")

KB_DIR: Final = ROOT_DIR / "docs" / "company-knowledge-base"

KnowledgeSource = Literal["qdrant", "local", "none"]


def _title(text: str, fallback: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first.lstrip("# ").strip() or fallback


def local_passages(documents: list[str], directory: Path = KB_DIR) -> list[dict[str, Any]]:
    passages: list[dict[str, Any]] = []
    for name in documents:
        path = directory / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("knowledge-base document %s is missing", name)
            continue
        passages.append(
            {
                "source_document": name,
                "section": _title(text, name),
                "text": text.strip(),
                "score": None,
            }
        )
    return passages


def search(queries: list[str], top_k: int) -> list[dict[str, Any]]:
    """Every hit for `queries` from Qdrant, best first, each chunk once."""
    from data.pipelines import rag

    hits: dict[tuple[Any, Any], dict[str, Any]] = {}
    for query in queries:
        for hit in rag.retrieve(query, k=top_k):
            key = (hit.get("source_document"), hit.get("chunk_index"))
            if key not in hits or hit["score"] > hits[key]["score"]:
                hits[key] = hit
    return sorted(hits.values(), key=lambda hit: hit["score"], reverse=True)


def retrieve_for(assignment: dict[str, Any], top_k: int) -> tuple[list[dict[str, Any]], str]:
    """(passages, source) for one department's assignment. Never raises."""
    try:
        hits = search(list(assignment.get("kb_queries") or []), top_k)
    except KnowledgeBaseError as error:
        logger.warning(
            "knowledge base unavailable for %s (%s); reading the documents instead",
            assignment.get("department_id"),
            type(error).__name__,
        )
        hits = []
    except Exception as error:  # noqa: BLE001 - optional context must not fail a draft
        logger.warning("knowledge base search failed (%s)", type(error).__name__)
        hits = []
    if hits:
        return hits, "qdrant"
    passages = local_passages(list(assignment.get("kb_documents") or []))
    return passages, ("local" if passages else "none")


def format_passages(passages: list[dict[str, Any]]) -> str:
    """The KNOWLEDGE BASE block of a prompt (the same layout as the RAG agent's)."""
    if not passages:
        return "(no knowledge-base passage is available)"
    blocks = []
    for number, chunk in enumerate(passages, 1):
        source = chunk.get("source_document", "unknown")
        section = chunk.get("section", "")
        blocks.append(f"[{number}] {source} — {section}\n{str(chunk.get('text', '')).strip()}")
    return "\n\n".join(blocks)


def passage_text(passages: list[dict[str, Any]]) -> str:
    return "\n".join(str(chunk.get("text", "")) for chunk in passages)
