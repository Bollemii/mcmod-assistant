"""Chargement des chunks produits par l'ingestion dans PostgreSQL, avec leurs embeddings.

Usage (depuis le dossier server/, base lancée par docker compose up -d) :
    python -m db.load                      # tous les serveurs, toutes les sources
    python -m db.load --server excalia --source wiki
    python -m db.load --force              # recharge tout (après un changement de modèle)

Fonctionnement :
1. servers.toml -> tables servers, server_addresses, sources, chat_roles.
2. Pour chaque source, lit data/servers/<serveur>/<source>/state.json et chunks.jsonl :
   - page nouvelle ou modifiée (hash différent) : ses chunks sont remplacés ;
   - page disparue : supprimée de la base (ses chunks avec) ;
   - page inchangée : rien n'est fait.
   Un chunk dont le texte n'a pas changé garde son vecteur (colonne text_hash) : seuls
   les textes vraiment nouveaux passent par le service d'embeddings.
3. sources.refreshed_at = maintenant.
"""
import argparse
import json
import logging
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from api.db import connect, migrate
from api.embeddings import Embedder, make_embedder, text_hash
from api.settings import SERVER_DIR, Settings, from_env

log = logging.getLogger("assist")
DATA_DIR = SERVER_DIR / "data" / "servers"

# Clés d'un chunk déjà rangées dans des colonnes (le reste va dans metadata).
CHUNK_COLUMNS = {"url", "title", "heading_path", "content", "id", "embed_text"}


def read_servers(path: Path) -> dict:
    return tomllib.loads(path.read_text("utf8"))


def sync_config(conn: psycopg.Connection, servers: dict) -> None:
    """Reflète servers.toml dans la base (ajouts et modifications ; rien n'est supprimé)."""
    for slug, conf in servers.items():
        server_id = conn.execute(
            """INSERT INTO servers (slug, name, season) VALUES (%s, %s, %s)
               ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, season = EXCLUDED.season
               RETURNING id""", (slug, conf["name"], conf.get("season"))).fetchone()[0]
        for address in conf.get("addresses", []):
            conn.execute("""INSERT INTO server_addresses (address, server_id) VALUES (%s, %s)
                            ON CONFLICT (address) DO UPDATE SET server_id = EXCLUDED.server_id""",
                         (address.lower(), server_id))
        for key, src in conf.get("sources", {}).items():
            conn.execute(
                """INSERT INTO sources (kind, server_id, config_key, season, priority)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (kind, server_id, config_key, season) DO UPDATE SET priority = EXCLUDED.priority""",
                (src["kind"], server_id, key, conf.get("season"), src.get("priority", 50)))
        for role, trust in conf.get("chat", {}).get("roles", {}).items():
            conn.execute("""INSERT INTO chat_roles (server_id, role, base_trust) VALUES (%s, %s, %s)
                            ON CONFLICT (server_id, role) DO UPDATE SET base_trust = EXCLUDED.base_trust""",
                         (server_id, role, trust))
    conn.commit()


def source_id(conn: psycopg.Connection, slug: str, key: str, season: str | None) -> int:
    row = conn.execute("""SELECT so.id FROM sources so JOIN servers se ON se.id = so.server_id
                          WHERE se.slug = %s AND so.config_key = %s AND so.season IS NOT DISTINCT FROM %s""",
                       (slug, key, season)).fetchone()
    if not row:
        raise KeyError(f"source {slug}/{key} absente de la base (sync_config d'abord)")
    return row[0]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_source(conn: psycopg.Connection, embedder: Embedder, slug: str, key: str, season: str | None,
                force: bool = False, data_dir: Path = DATA_DIR) -> dict:
    """Charge une source ; renvoie le bilan {added, changed, removed, unchanged, embedded}."""
    folder = data_dir / slug / key
    state_path = folder / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"{state_path} introuvable : lance d'abord l'ingestion de {slug}/{key}")
    state = json.loads(state_path.read_text("utf8"))
    chunks_by_url = defaultdict(list)
    for c in _read_jsonl(folder / "chunks.jsonl"):
        chunks_by_url[c["url"].split("#")[0]].append(c)

    sid = source_id(conn, slug, key, season)
    in_db = {url: (doc_id, h) for doc_id, url, h in
             conn.execute("SELECT id, url, content_hash FROM documents WHERE source_id = %s", (sid,))}

    to_write = [url for url, s in state.items() if force or url not in in_db or in_db[url][1] != s["hash"]]
    removed = [url for url in in_db if url not in state]
    # Bilan sur le contenu : avec force, une page rechargée mais identique reste « inchangée ».
    added = sum(1 for u in to_write if u not in in_db)
    changed = sum(1 for u in to_write if u in in_db and in_db[u][1] != state[u]["hash"])
    stats = {"added": added, "changed": changed, "removed": len(removed),
             "unchanged": len(state) - added - changed, "embedded": 0}

    # Embeddings d'abord (hors transaction : avec le quota gratuit, cela peut prendre des minutes).
    new_chunks = [c for url in to_write for c in chunks_by_url.get(url, [])]
    hashes = [text_hash(c["embed_text"]) for c in new_chunks]
    known = {}
    if hashes:
        known = dict(conn.execute("""SELECT DISTINCT ON (text_hash) text_hash, embedding FROM chunks
                                     WHERE embed_model = %s AND text_hash = ANY(%s)""",
                                  (embedder.model, list(set(hashes)))).fetchall())
    conn.commit()
    missing = list(dict.fromkeys(h for h in hashes if h not in known))
    if missing:
        texts = {h: c["embed_text"] for h, c in zip(hashes, new_chunks)}
        log.info("%s/%s : %d textes à embedder", slug, key, len(missing))
        vectors = embedder.embed_documents([texts[h] for h in missing])
        known.update((h, np.asarray(v, dtype=np.float32)) for h, v in zip(missing, vectors))
    stats["embedded"] = len(missing)

    with conn.transaction():
        if removed:
            conn.execute("DELETE FROM documents WHERE source_id = %s AND url = ANY(%s)", (sid, removed))
        for url in to_write:
            s = state[url]
            page_chunks = chunks_by_url.get(url, [])
            first = page_chunks[0] if page_chunks else {}
            category = first.get("category_name") or first.get("version")
            doc_id = conn.execute(
                """INSERT INTO documents (source_id, url, title, category, content_hash, published_at, fetched_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (source_id, url) DO UPDATE SET title = EXCLUDED.title, category = EXCLUDED.category,
                     content_hash = EXCLUDED.content_hash, published_at = EXCLUDED.published_at, fetched_at = now()
                   RETURNING id""",
                (sid, url, s.get("title") or first.get("title") or url, category, s["hash"],
                 _date(s.get("published_at")))).fetchone()[0]
            conn.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
            for c in page_chunks:
                h = text_hash(c["embed_text"])
                conn.execute(
                    """INSERT INTO chunks (document_id, chunk_key, url, heading_path, content, embedding,
                                           embed_model, text_hash, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (doc_id, f"{slug}:{key}:{c['id']}", c["url"], c["heading_path"], c["content"], known[h],
                     embedder.model, h, Jsonb({k: v for k, v in c.items() if k not in CHUNK_COLUMNS})))
        conn.execute("UPDATE sources SET refreshed_at = now() WHERE id = %s", (sid,))
    return stats


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="serveur de servers.toml (défaut : tous)")
    ap.add_argument("--source", help="clé de la source, ex. wiki (défaut : toutes)")
    ap.add_argument("--force", action="store_true", help="recharger même les pages inchangées")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    settings: Settings = from_env()
    if not settings.database_url:
        raise SystemExit("ASSIST_DATABASE_URL n'est pas défini (voir server/.env.example)")
    servers = read_servers(settings.servers_file)
    embedder = make_embedder(settings)
    with connect(settings.database_url) as conn:
        migrate(conn)
        sync_config(conn, servers)
        for slug, conf in servers.items():
            if args.server and slug != args.server:
                continue
            for key in conf.get("sources", {}):
                if args.source and key != args.source:
                    continue
                stats = load_source(conn, embedder, slug, key, conf.get("season"), force=args.force)
                print(f"{slug}/{key} : {stats['added']} nouvelles pages, {stats['changed']} modifiées, "
                      f"{stats['removed']} supprimées, {stats['unchanged']} inchangées ; "
                      f"{stats['embedded']} textes embeddés ({embedder.model})")


if __name__ == "__main__":
    main()
