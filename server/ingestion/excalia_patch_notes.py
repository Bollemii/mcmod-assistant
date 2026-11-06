"""Ingestion des notes de patch Excalia -> chunks JSONL prêts à être embeddés.

Usage : python excalia_patch_notes.py [--server excalia] [--all-seasons]
Sortie : server/data/servers/<serveur>/patch_notes/ (chunks.jsonl, state.json, raw/<slug>.json)

- Liste : l'API ne renvoie que les 20 dernières notes (take=20, pas de pagination) ;
  on complète avec les liens /patch-notes/<slug> du sitemap.
- Détail : GET <api>/<slug>, contenu HTML découpé par section (h2).
- Seules les notes de la saison configurée sont gardées (sources.patch_notes.version_prefix),
  sauf avec --all-seasons.
- Une note déjà récupérée n'est pas retéléchargée : elle est relue depuis raw/.
  Une note est redemandée si sa date de publication ou son résumé changent dans la liste.
- La date de publication est mise dans chaque chunk et dans le texte embeddé :
  une note de patch récente l'emporte sur une page de wiki pas encore mise à jour.
"""
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from common import get_json, load_server, load_state, save_outputs, session, source_dir
from html_chunker import chunk_html

LIST_TAKE = 20  # maximum accepté par l'API


def list_slugs(conf: dict) -> dict[str, dict]:
    """slug -> entrée de la liste (ou {} si le slug ne vient que du sitemap)."""
    entries = {p["slug"]: p for p in get_json(conf["api"], take=LIST_TAKE)["patchNotes"]}
    if sitemap := conf.get("sitemap"):
        resp = session.get(sitemap, timeout=60)
        resp.raise_for_status()
        prefix = re.escape(conf["public_url"])
        for loc in ET.fromstring(resp.content).iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
            if m := re.fullmatch(prefix + r"([^/]+)", loc.text or ""):
                entries.setdefault(m.group(1), {})
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="excalia")
    ap.add_argument("--all-seasons", action="store_true")
    args = ap.parse_args()

    server = load_server(args.server)
    conf = server["sources"]["patch_notes"]
    out_dir = source_dir(server["slug"], "patch_notes")
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    entries = list_slugs(conf)
    print(f"{len(entries)} notes de patch trouvées (liste API + sitemap)")

    old_state = load_state(out_dir / "state.json")
    state, chunks, fetched = {}, [], 0
    for slug, entry in entries.items():
        raw_path = raw_dir / f"{slug}.json"
        cached = json.loads(raw_path.read_text("utf8")) if raw_path.exists() else None
        stale = cached is None or (entry and (entry.get("publishedAt"), entry.get("excerpt"))
                                   != (cached.get("publishedAt"), cached.get("excerpt")))
        if stale:
            note = get_json(f"{conf['api']}/{slug}")["patchNote"]
            raw_path.write_text(json.dumps(note, ensure_ascii=False), "utf8")
            fetched += 1
        else:
            note = cached

        version = note.get("version") or ""
        if not args.all_seasons and not version.startswith(conf["version_prefix"]):
            continue
        url = conf["public_url"] + slug
        html = note.get("content") or ""
        published = note["publishedAt"]
        day = datetime.fromisoformat(published.replace("Z", "+00:00")).strftime("%d/%m/%Y")
        state[url] = {"hash": hashlib.sha256(html.encode()).hexdigest(),
                      "title": note["title"], "published_at": published}
        meta = {"server": server["slug"], "source": "patch_notes", "version": version,
                "published_at": published, "author": (note.get("author") or {}).get("minecraftName")}
        chunks += chunk_html(html, url, note["title"], meta, title_prefix=f"Patch {version} ({day})")

    print(f"{fetched} notes téléchargées, {len(entries) - fetched} relues depuis raw/")
    chunks.sort(key=lambda c: c["published_at"], reverse=True)
    save_outputs(out_dir, chunks, state, old_state)


if __name__ == "__main__":
    main()
