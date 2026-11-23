"""Concept graph: grounding, teaching-order validation, rendering (pure logic)."""
from notetaker.graph import normalize_graph, to_mermaid
from notetaker.notion import build_blocks

SECTIONS = [
    {"title": "Neurons", "start_sec": 0, "bullets": ["A neuron holds an activation between 0 and 1."]},
    {"title": "Weights and biases", "start_sec": 200, "bullets": ["Each connection has a weight; a bias shifts it."]},
    {"title": "Sigmoid function", "start_sec": 400, "bullets": ["The sigmoid squashes the weighted sum."]},
    {"title": "Matrix notation", "start_sec": 700, "bullets": ["Weights form a matrix; activations a vector."]},
]


def test_graph_is_grounded_in_teaching_order_and_cleaned():
    raw = {"concepts": [
        {"id": "a", "name": "Neuron", "definition": "Holds a number.", "first_sec": 999},
        {"id": "b", "name": "Weights", "definition": "Connection strengths.", "first_sec": 5},
        {"id": "c", "name": "Sigmoid function", "definition": "Squashes values.", "first_sec": 400},
        {"id": "d", "name": "Matrix notation", "definition": "Compact form.", "first_sec": 700},
        {"id": "e", "name": "neurons", "definition": "dup"},
    ], "relations": [
        {"from": "a", "to": "b", "type": "prerequisite_of", "label": "needed for"},
        {"from": "d", "to": "a", "type": "prerequisite_of", "label": "x"},       # taught after → dropped
        {"from": "b", "to": "c", "type": "leads_to", "label": "fed | into"},
        {"from": "c", "to": "c", "type": "uses"},                                # self loop
        {"from": "zz", "to": "a", "type": "uses"},                               # unknown id
        {"from": "b", "to": "d", "type": "weird", "label": ""},                  # unknown type → leads_to
    ]}
    g = normalize_graph(raw, SECTIONS, 1000)
    names = [c["name"] for c in g["concepts"]]
    assert names == ["Neuron", "Weights", "Sigmoid function", "Matrix notation"]  # dup removed, time order
    assert [c["first_sec"] for c in g["concepts"]] == [0, 200, 400, 700]          # grounded, not LLM's guess
    pairs = {(r["from"], r["to"], r["type"]) for r in g["relations"]}
    assert ("c1", "c2", "prerequisite_of") in pairs and ("c2", "c4", "leads_to") in pairs
    assert len(g["relations"]) == 3 and g["dropped_by_teaching_order"] == 1
    assert all("|" not in r["label"] for r in g["relations"])
    m = to_mermaid(g)
    assert m.startswith("flowchart LR") and 'c1["Neuron"]' in m and "c1 ==>|needed for| c2" in m


def test_concept_map_and_related_are_rendered_as_blocks_not_images():
    g = normalize_graph({"concepts": [{"id": "a", "name": "Neuron"}, {"id": "b", "name": "Weights"}],
                         "relations": [{"from": "a", "to": "b", "type": "prerequisite_of"}]}, SECTIONS, 1000)
    syn = {"sections": SECTIONS, "tldr": [], "key_terms": [], "review_questions": [], "graph": g}
    related = [{"page_id": "11111111-2222-3333-4444-555555555555", "shared": ["Neuron"]}]
    blocks = build_blocks({"video_id": "dQw4w9WgXcQ", "job_id": "j"}, syn, [], {}, [], related)
    types = [b["type"] for b in blocks]
    assert "image" not in types  # Constraint B: only real frames are images
    code = next(b for b in blocks if b["type"] == "code")
    assert code["code"]["language"] == "mermaid"
    mention = [b for b in blocks if b["type"] == "bulleted_list_item"
               and b["bulleted_list_item"]["rich_text"][0]["type"] == "mention"]
    assert mention and mention[0]["bulleted_list_item"]["rich_text"][0]["mention"]["page"]["id"].startswith("1111")


def test_first_explained_prefers_agreeing_or_titled_section_over_intro_mention():
    from notetaker.graph import _first_mention
    secs = [{"title": "Intro", "start_sec": 0, "bullets": ["We cover the chain rule and gradients."]},
            {"title": "Derivatives", "start_sec": 120, "bullets": ["The chain rule multiplies local derivatives."]},
            {"title": "Chain rule in detail", "start_sec": 300, "bullets": ["Worked example."]}]
    assert _first_mention("Chain rule", secs, llm_t=130) == 120   # LLM estimate agrees with a mentioning section
    assert _first_mention("Chain rule", secs, llm_t=None) == 300  # else the section titled after it
    assert _first_mention("Gradients", secs, llm_t=999) == 0      # else first mention
    assert _first_mention("Softmax", secs, llm_t=10) is None
