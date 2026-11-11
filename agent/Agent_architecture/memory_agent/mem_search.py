"""Deterministic retrieval helper for the memory agent — no LLM, no agent state.
Own copy, kept separate from analysis_team/supervisor_agent/sim_search.py
(decisions rule 44)."""

_MODEL = None  # lazily loaded so importing this module never downloads/loads anything


def _model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2")
    return _MODEL


def top_matches(question: str, candidates: list[dict], k: int = 5) -> list[dict]:
    """candidates: [{"node_id": str, "question": str}, ...].
    Returns the k most similar by cosine similarity on sentence embeddings,
    most similar first, each dict extended with "score": float.
    Empty candidates -> []."""
    if not candidates:
        return []

    from sklearn.metrics.pairwise import cosine_similarity

    model = _model()
    texts = [question] + [c["question"] for c in candidates]
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
