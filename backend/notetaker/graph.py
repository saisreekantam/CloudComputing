"""Concept graph (knowledge graph) per video + a cross-video concept index.

Per video
  The LLM proposes concepts and typed relations from the finished section notes. Research on
  instructional knowledge graphs (e.g. InstructKG) finds transcript-only extraction noisy, so the
  output is grounded before use:
    * every concept is anchored to the earliest section that actually mentions it (teaching order);
    * "prerequisite_of" edges that contradict teaching order (A taught well after B) are dropped;
    * unknown ids, self-loops, duplicates and isolated noise are removed; sizes are capped.
  The graph is rendered in Notion as a Mermaid flowchart (a code block that Notion draws as a
  diagram — not an image, so Constraint B "every image is a real frame" is untouched) plus a
  concept index with "first explained at [mm:ss]" deep links.

Across videos
  Each published page registers its concept set under the Notion workspace (Redis hash keyed by
  video id, so retries/replays overwrite rather than duplicate). New pages link to earlier pages
  that share concepts: "Related videos in your notes".
"""
from __future__ import annotations

from typing import Any

REL_TYPES = {"prerequisite_of", "part_of", "is_a", "uses", "leads_to", "contrasts_with", "example_of"}
REL_DEFAULT_LABEL = {"prerequisite_of": "needed for", "part_of": "part of", "is_a": "is a", "uses": "uses",
                     "leads_to": "leads to", "contrasts_with": "vs", "example_of": "example of"}
MAX_CONCEPTS = 14
MAX_RELATIONS = 22


def graph_prompt(title: str, sections: list[dict], key_terms: list[dict], language: str,
                 max_chars: int = 9000) -> str:
    """Prompt asking the LLM for {"concepts": [{id, name, definition, first_sec}], "relations": [{from, to, type, label}]}."""
    raise NotImplementedError


def normalize_graph(raw: dict, sections: list[dict], duration: int) -> dict:
    """Validate + ground an LLM concept graph (teaching order, caps, dedupe).
    Returns {"concepts": [...], "relations": [...], "dropped_by_teaching_order": int}."""
    raise NotImplementedError


def to_mermaid(graph: dict) -> str:
    """Mermaid `flowchart LR` source for the graph (Notion renders it as a diagram)."""
    raise NotImplementedError


# --------------------------------------------------------------------------- cross-video index
async def related_pages(r, token: str, video_id: str, graph: dict, limit: int = 5) -> list[dict]:
    """Earlier pages in the same workspace that share concepts with `graph` (never `video_id` itself)."""
    raise NotImplementedError


async def register_page(r, token: str, video_id: str, page: dict, title: str, graph: dict) -> None:
    raise NotImplementedError


async def forget_page(r, token: str, video_id: str) -> None:
    raise NotImplementedError


def as_public(graph: dict) -> dict[str, Any]:
    """Graph without internal fields (for the API / results)."""
    return {"concepts": [{k: c.get(k) for k in ("id", "name", "definition", "first_sec")} for c in graph.get("concepts", [])],
            "relations": graph.get("relations", [])}
