"""Recherche hybride des extraits utiles à une question.

Deux classements, fusionnés par RRF (Reciprocal Rank Fusion : score = somme de
1 / (60 + rang) sur les deux listes) :
- similarité des vecteurs (pgvector, distance cosinus) : comprend les reformulations ;
- plein texte PostgreSQL en français, sans accents : retrouve les noms exacts
  (« Base Rouge », « /is upgrade ») que les vecteurs ratent parfois.
Les termes de la question sont combinés en OU : une question de joueur contient
souvent des mots absents de la page, un ET ne trouverait presque rien.

Filtres : sources actives du serveur (ou communes, server_id NULL), de la saison en
cours, non expirées. Au plus `max_per_doc` extraits par page, pour varier les sources.
"""
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import psycopg

RRF_K = 60
CANDIDATES = 40     # taille de chaque classement avant fusion


@dataclass
class ContextChunk:
    id: int
    url: str
    title: str                   # titre de la page
    heading_path: list[str]
    content: str
    kind: str                    # server_wiki, server_news…
    published_at: datetime | None
    score: float = 0.0
    ranks: dict = field(default_factory=dict)
    key: str = ""                # chunk_key : stable d'un rechargement à l'autre, contrairement à id

    @property
    def section(self) -> str:
        return " > ".join(self.heading_path)


SEARCH_SQL = """
WITH eligible AS (
  SELECT c.id
  FROM chunks c
  JOIN documents d ON d.id = c.document_id
  JOIN sources s ON s.id = d.source_id
  LEFT JOIN servers sv ON sv.id = s.server_id
  WHERE s.active
    AND (s.server_id IS NULL OR sv.slug = %(server)s)
    AND (s.season IS NULL OR s.season = sv.season)
    AND (d.expires_at IS NULL OR d.expires_at > now())
),
terms AS (
  -- termes de la question (racines françaises, sans accents ni mots vides), pondérés par
  -- leur rareté (IDF) : « broyeur » compte plus que « combien » ou « île »
  SELECT t.tq, t.tq_title,
         ln((SELECT count(*) FROM chunks)::float / (1 + (SELECT count(*) FROM chunks c WHERE c.tsv @@ t.tq))) AS idf
  FROM (SELECT DISTINCT quote_literal(lexeme)::tsquery AS tq, (quote_literal(lexeme) || ':A')::tsquery AS tq_title
        FROM unnest(to_tsvector('french', f_unaccent(%(text)s)))) t
),
vec AS (
  SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> %(vec)s) AS rank
  FROM chunks c
  WHERE %(vec)s::vector IS NOT NULL AND c.id IN (SELECT id FROM eligible)
  ORDER BY c.embedding <=> %(vec)s
  LIMIT %(n)s
),
fts AS (
  -- somme des IDF des termes présents, doublée quand le terme est dans un titre de section
  SELECT c.id, row_number() OVER (ORDER BY sum(t.idf * (1 + (c.tsv @@ t.tq_title)::int)) DESC, c.id) AS rank
  FROM chunks c JOIN terms t ON c.tsv @@ t.tq
  WHERE c.id IN (SELECT id FROM eligible)
  GROUP BY c.id
  ORDER BY rank
  LIMIT %(n)s
),
fused AS (
  SELECT id, sum(1.0 / (%(k)s + rank)) AS score,
         min(rank) FILTER (WHERE src = 'vec') AS vec_rank,
         min(rank) FILTER (WHERE src = 'fts') AS fts_rank
  FROM (SELECT id, rank, 'vec' AS src FROM vec UNION ALL SELECT id, rank, 'fts' FROM fts) r
  GROUP BY id
)
SELECT c.id, c.url, d.title, c.heading_path, c.content, s.kind::text, d.published_at, d.id,
       f.score, f.vec_rank, f.fts_rank, c.chunk_key
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
JOIN sources s ON s.id = d.source_id
ORDER BY f.score DESC, s.priority DESC
"""


def search(conn: psycopg.Connection, question: str, query_vec: list[float] | None, server: str | None,
           top_k: int = 8, max_per_doc: int = 3) -> list[ContextChunk]:
    """Extraits les plus utiles, du plus pertinent au moins pertinent.

    query_vec None (service d'embeddings indisponible) : plein texte seul.
    """
    vec = np.asarray(query_vec, dtype=np.float32) if query_vec is not None else None
    rows = conn.execute(SEARCH_SQL, {"server": server, "text": question, "vec": vec,
                                     "n": CANDIDATES, "k": RRF_K}).fetchall()
    results, per_doc = [], {}
    for cid, url, title, path, content, kind, published, doc_id, score, vrank, frank, key in rows:
        if per_doc.get(doc_id, 0) >= max_per_doc:
            continue
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        results.append(ContextChunk(cid, url, title, list(path), content, kind, published, float(score),
                                    {"vec": vrank, "fts": frank}, key))
        if len(results) >= top_k:
            break
    return results
