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

import hashlib
import json
import re
from typing import Any

from .youtube import fmt_ts

REL_TYPES = {"prerequisite_of", "part_of", "is_a", "uses", "leads_to", "contrasts_with", "example_of"}
REL_DEFAULT_LABEL = {"prerequisite_of": "needed for", "part_of": "part of", "is_a": "is a", "uses": "uses",
                     "leads_to": "leads to", "contrasts_with": "vs", "example_of": "example of"}
MAX_CONCEPTS = 14
MAX_RELATIONS = 22


def graph_prompt(title: str, sections: list[dict], key_terms: list[dict], language: str,
                 max_chars: int = 9000) -> str:
    lines: list[str] = []
    for s in sections:
        lines.append(f"[{fmt_ts(s['start_sec'])}] {s['title']}")
        lines += [f"  - {b}" for b in s.get("bullets", [])[:6]]
    outline = "\n".join(lines)[:max_chars]
    terms = "; ".join(f"{t['term']}: {t.get('definition', '')}" for t in key_terms[:12])
    return f"""These are study notes for the video "{title}", in teaching order:

{outline}

Key terms: {terms}

Build a concept map of the ideas the video teaches. Return JSON:
{{"concepts": [{{"id": "c1", "name": string, "definition": string, "first_sec": integer}}, ...],
 "relations": [{{"from": "c1", "to": "c2", "type": "prerequisite_of|part_of|is_a|uses|leads_to|contrasts_with|example_of", "label": string}}, ...]}}
Rules:
- concepts: 6-{MAX_CONCEPTS} central ideas, techniques, tools or terms (short noun phrases, 1-4 words). Not people,
  channels or filler. definition: one sentence grounded in the notes. first_sec: when it is first explained.
- relations: 6-20, each stated or clearly implied by the notes. prerequisite_of = you need A before B.
  label: a 1-4 word verb phrase. Every concept should take part in at least one relation.
- Write names, definitions and labels in {language}."""


def _norm(name: str) -> str:
    n = re.sub(r"[^\w\s+#.-]", " ", name.lower())
    n = re.sub(r"\s+", " ", n).strip()
    if len(n) > 4 and n.endswith("s") and not n.endswith("ss"):
        n = n[:-1]
    return n


def _first_mention(name: str, sections: list[dict], llm_t: int | None = None) -> int | None:
    """Start of the section where the concept is *explained*, grounded in the notes:
    1. a section that mentions it and contains the LLM's own estimate (both sources agree);
    2. else the first section whose title names it;
    3. else the first section that mentions it at all. None if the notes never mention it."""
    key = _norm(name)
    if not key:
        return None
    toks = [t for t in key.split() if len(t) > 2] or key.split()

    def has(text: str) -> bool:
        hay = _norm(text)
        return key in hay or (len(toks) > 1 and all(t in hay for t in toks))

    mentioning = [i for i, s in enumerate(sections) if has(s.get("title", "") + " " + " ".join(s.get("bullets", [])))]
    if not mentioning:
        return None
    if llm_t is not None:
        for i in mentioning:
            end = sections[i + 1]["start_sec"] if i + 1 < len(sections) else float("inf")
            if sections[i]["start_sec"] <= llm_t < end:
                return int(sections[i]["start_sec"])
    for i in mentioning:
        if has(sections[i].get("title", "")):
            return int(sections[i]["start_sec"])
    return int(sections[mentioning[0]]["start_sec"])


def normalize_graph(raw: dict, sections: list[dict], duration: int) -> dict:
    """Validate + ground an LLM concept graph. Returns {"concepts": [...], "relations": [...]}."""
    concepts: list[dict] = []
    ids: dict[str, str] = {}
    seen_names: set[str] = set()
    for c in raw.get("concepts") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()[:60]
        key = _norm(name)
        if not name or key in seen_names:
            continue
        cid = f"c{len(concepts) + 1}"
        ids[str(c.get("id") or name)] = cid
        ids.setdefault(name, cid)
        seen_names.add(key)
        try:
            llm_t = int(round(float(c.get("first_sec"))))
        except (TypeError, ValueError):
            llm_t = 0
        grounded = _first_mention(name, sections, llm_t)
        t = grounded if grounded is not None else min(max(llm_t, 0), max(duration - 1, 0))
        concepts.append({"id": cid, "name": name, "key": key, "definition": str(c.get("definition") or "").strip()[:300],
                         "first_sec": t, "grounded": grounded is not None})
        if len(concepts) >= MAX_CONCEPTS:
            break
    by_id = {c["id"]: c for c in concepts}
    relations: list[dict] = []
    seen_rel: set[tuple[str, str]] = set()
    dropped_order = 0
    for r in raw.get("relations") or []:
        if not isinstance(r, dict):
            continue
        a, b = ids.get(str(r.get("from"))), ids.get(str(r.get("to")))
        if not a or not b or a == b or (a, b) in seen_rel:
            continue
        rtype = str(r.get("type") or "").strip().lower()
        rtype = rtype if rtype in REL_TYPES else "leads_to"
        if (rtype == "prerequisite_of" and by_id[a]["grounded"] and by_id[b]["grounded"]
                and by_id[a]["first_sec"] > by_id[b]["first_sec"] + 30):
            dropped_order += 1  # contradicts teaching order: a prerequisite is taught first
            continue
        label = re.sub(r"[|\"<>{}\[\]]", "", str(r.get("label") or "")).strip()[:30] or REL_DEFAULT_LABEL[rtype]
        relations.append({"from": a, "to": b, "type": rtype, "label": label})
        seen_rel.add((a, b))
        if len(relations) >= MAX_RELATIONS:
            break
    linked = {x for r in relations for x in (r["from"], r["to"])}
    # Keep isolated concepts only while the map is small; they still appear in the concept index.
    if len(concepts) > 8:
        concepts = [c for c in concepts if c["id"] in linked]
    concepts.sort(key=lambda c: c["first_sec"])
    return {"concepts": concepts, "relations": [r for r in relations if r["from"] in {c["id"] for c in concepts}
                                                and r["to"] in {c["id"] for c in concepts}],
            "dropped_by_teaching_order": dropped_order}


def to_mermaid(graph: dict) -> str:
    def esc(s: str) -> str:
        return s.replace('"', "'").replace("\n", " ")
    lines = ["flowchart LR"]
    for c in graph["concepts"]:
        lines.append(f'  {c["id"]}["{esc(c["name"])}"]')
    for r in graph["relations"]:
        arrow = "-.->" if r["type"] in ("example_of", "contrasts_with") else "==>" if r["type"] == "prerequisite_of" else "-->"
        lines.append(f'  {r["from"]} {arrow}|{esc(r["label"])}| {r["to"]}')
    return "\n".join(lines)


# --------------------------------------------------------------------------- cross-video index
def workspace_key(token: str) -> str:
    return "kg:" + hashlib.sha256(("kg::" + token).encode()).hexdigest()[:16]


def _words(text: str) -> str:
    """Word-normalised text for phrase matching (lower-case, punctuation-free, plurals folded)."""
    out = []
    for w in re.sub(r"[^\w\s+#-]", " ", text.lower()).split():
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " " + " ".join(out) + " "


def _concept_text(graph: dict) -> str:
    return _words(" ".join(f"{c['name']}. {c.get('definition', '')}" for c in graph.get("concepts", [])))[:6000]


def shared_concepts(my_names: list[str], my_text: str, their_names: list[str], their_text: str) -> list[str]:
    """Concepts of either video that the other one also covers (name appears in its concepts/definitions)."""
    out: dict[str, str] = {}
    for names, other in ((their_names, my_text), (my_names, their_text)):
        for n in names:
            key = _words(n).strip()
            if len(key) >= 4 and f" {key} " in other and key not in out:
                out[key] = n
    return list(out.values())


async def related_pages(r, token: str, video_id: str, graph: dict, limit: int = 5) -> list[dict]:
    names = [c["name"] for c in graph.get("concepts", [])]
    if not names:
        return []
    text = _concept_text(graph)
    out = []
    for vid, raw in (await r.hgetall(workspace_key(token))).items():
        vid = vid.decode() if isinstance(vid, bytes) else vid
        if vid == video_id:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        shared = shared_concepts(names, text, entry.get("names", []), entry.get("text", ""))
        if shared:
            out.append({**entry, "video_id": vid, "shared": shared})
    out.sort(key=lambda e: (-len(e["shared"]), e.get("title", "")))
    return out[:limit]


async def register_page(r, token: str, video_id: str, page: dict, title: str, graph: dict) -> None:
    names = [c["name"] for c in graph.get("concepts", [])]
    if names:
        await r.hset(workspace_key(token), video_id, json.dumps(
            {"page_id": page["id"], "url": page.get("url", ""), "title": title, "names": names,
             "text": _concept_text(graph)}))


async def forget_page(r, token: str, video_id: str) -> None:
    await r.hdel(workspace_key(token), video_id)


def as_public(graph: dict) -> dict[str, Any]:
    """Graph without internal fields (for the API / results)."""
    return {"concepts": [{k: c[k] for k in ("id", "name", "definition", "first_sec")} for c in graph.get("concepts", [])],
            "relations": graph.get("relations", [])}
