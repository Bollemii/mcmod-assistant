"""Ingestion du wiki Excalia via son API -> chunks JSONL prêts à être embeddés.

Usage : python excalia_wiki.py [--server excalia] [--from-file FICHIER]
Sortie : server/data/servers/<serveur>/wiki/ (chunks.jsonl, state.json, wiki_raw.json)

- Un seul appel à l'API (config : sources.wiki.api) renvoie tout le wiki, toutes
  saisons ; on garde celle de la config (sources.wiki.wiki_key). La réponse brute
  est conservée pour pouvoir re-découper sans réseau (--from-file).
- L'API ne donne pas de date de modification : les pages nouvelles, modifiées ou
  supprimées sont détectées par un hash du contenu (state.json), pour ne recalculer
  ensuite que les embeddings qui ont changé.
"""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin

from common import get_json, load_server, load_state, save_outputs, source_dir
from html_chunker import chunk_html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="excalia")
    ap.add_argument("--from-file", help="relire une réponse d'API enregistrée au lieu d'appeler l'API")
    args = ap.parse_args()

    server = load_server(args.server)
    conf = server["sources"]["wiki"]
    out_dir = source_dir(server["slug"], "wiki")

    if args.from_file:
        data = json.loads(Path(args.from_file).read_text("utf8"))
    else:
        data = get_json(conf["api"])
        (out_dir / "wiki_raw.json").write_text(json.dumps(data, ensure_ascii=False), "utf8")
    wikis = {w["key"]: w["name"] for w in data["wikis"]}
    print(f"API : {data['totalPages']} pages, wikis disponibles : {', '.join(wikis)}")
    if conf["wiki_key"] not in wikis:
        raise SystemExit(f"Wiki inconnu : {conf['wiki_key']}")

    base = urljoin(conf["api"], "/wiki/")
    old_state = load_state(out_dir / "state.json")
    state, chunks = {}, []
    for cat in data["categories"]:
        if cat["wikiKey"] != conf["wiki_key"]:
            continue
        meta = {"server": server["slug"], "source": "wiki", "wiki": cat["wikiKey"],
                "category": cat["slug"], "category_name": cat["name"]}
        for page in cat["pages"]:
            url = f"{base}{cat['slug']}/{page['slug']}"
            html = page["content"] or ""
            state[url] = {"hash": hashlib.sha256(html.encode()).hexdigest(), "title": page["title"]}
            chunks += chunk_html(html, url, page["title"], meta)

    save_outputs(out_dir, chunks, state, old_state)


if __name__ == "__main__":
    main()
