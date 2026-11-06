"""Outils partagés par les scripts d'ingestion : HTTP, configuration des serveurs, sorties."""
import json
import tomllib
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # utilise le magasin de certificats de l'OS (utile sous Windows / derrière un proxy)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

SERVER_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SERVER_DIR / "config" / "servers.toml"
DATA_DIR = SERVER_DIR / "data"
USER_AGENT = "ExcaliaAssistBot/0.1 (assistant /assist, usage personnel)"

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
# Les sites passent souvent par Cloudflare et renvoient parfois des 502/503/504 :
# on réessaie avec un délai croissant (2 s, 4 s, 8 s…) avant d'abandonner.
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",))))


def get_json(url: str, **params) -> dict:
    resp = session.get(url, params=params or None, timeout=60)
    resp.raise_for_status()
    return resp.json()


def load_server(slug: str) -> dict:
    servers = tomllib.loads(CONFIG_PATH.read_text("utf8"))
    if slug not in servers:
        raise SystemExit(f"Serveur inconnu : {slug} (connus : {', '.join(servers)})")
    return {"slug": slug, **servers[slug]}


def source_dir(server: str, source: str) -> Path:
    """Dossier de sortie d'une source propre à un serveur."""
    path = DATA_DIR / "servers" / server / source
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state(path: Path) -> dict:
    return json.loads(path.read_text("utf8")) if path.exists() else {}


def save_outputs(out_dir: Path, chunks: list[dict], state: dict, old_state: dict) -> None:
    """Écrit chunks.jsonl et state.json, puis affiche le bilan des changements."""
    with (out_dir / "chunks.jsonl").open("w", encoding="utf8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    (out_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf8")
    added = len(set(state) - set(old_state))
    changed = sum(1 for k in set(state) & set(old_state) if state[k]["hash"] != old_state[k]["hash"])
    removed = len(set(old_state) - set(state))
    print(f"{len(state)} documents : {added} nouveaux, {changed} modifiés, {removed} supprimés")
    print(f"{len(chunks)} chunks -> {out_dir / 'chunks.jsonl'}")
