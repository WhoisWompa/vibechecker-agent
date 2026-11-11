"""Deterministic retrieval helper next to memory.py — no LLM, no agent state.
Shared by the memory agent and (later) the supervisor-loop wiring for the
dedup funnel (decisions rule 44): sim search first, an LLM judges the top-k.
"""

_MODEL = None  # lazily loaded so importing this module never downloads/loads anything


def _model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _MODEL


def top_candidates(question: str, candidates: list[dict], k: int = 5) -> list[dict]:
    """candidates: [{"node_id": str, "question": str, ...}, ...] — each candidate is
    embedded on its "text" field if present, else falls back to "question" (the
    memory agent passes question-only dicts; the supervisor's dedup flow passes
    dicts with a "text" key holding question+prediction).
    Returns the k most similar by cosine similarity on sentence embeddings,
    most similar first, each dict extended with "score": float.
    Empty candidates -> []."""
    if not candidates:
        return []

    from sklearn.metrics.pairwise import cosine_similarity

    model = _model()
    texts = [question] + [c.get("text") or c["question"] for c in candidates]
    embeddings = model.encode(texts)
    query_embedding = embeddings[0:1]
    candidate_embeddings = embeddings[1:]
    scores = cosine_similarity(query_embedding, candidate_embeddings)[0]

    scored = [
        {**candidate, "score": float(score)}
        for candidate, score in zip(candidates, scores)
    ]
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:k]


def top_candidates_multi(queries: list[str], candidates: list[dict], k: int = 5) -> list[dict]:
    """Runs the same retrieval once per query string (k each), merges the results
    dropping duplicate node_ids (keeping each node's highest score), and returns
    the merged list sorted by score descending. Candidates are encoded once and
    compared against every query embedding, not re-encoded per query.
    Empty candidates -> []."""
    if not candidates or not queries:
        return []

    from sklearn.metrics.pairwise import cosine_similarity

    model = _model()
    candidate_texts = [c.get("text") or c["question"] for c in candidates]
    all_texts = list(queries) + candidate_texts
    embeddings = model.encode(all_texts)
    query_embeddings = embeddings[: len(queries)]
    candidate_embeddings = embeddings[len(queries):]
    all_scores = cosine_similarity(query_embeddings, candidate_embeddings)

    best: dict[str, dict] = {}
    for scores in all_scores:
        scored = [
            {**candidate, "score": float(score)}
            for candidate, score in zip(candidates, scores)
        ]
        scored.sort(key=lambda c: c["score"], reverse=True)
        for entry in scored[:k]:
            node_id = entry["node_id"]
            if node_id not in best or entry["score"] > best[node_id]["score"]:
                best[node_id] = entry

    merged = list(best.values())
    merged.sort(key=lambda c: c["score"], reverse=True)
    return merged
