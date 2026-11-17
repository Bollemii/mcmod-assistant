"""Chargement des données de Minecraft vanilla (game_data.json) dans PostgreSQL.

Usage (depuis server/, base lancée par docker compose up -d) :
    python -m ingestion.minecraft_data --version 26.2     # extraction locale, sans réseau
    python -m db.load_minecraft --version 26.2

Aucun embedding ni appel externe : objets, noms et recettes sont des données structurées.

- mc_versions : la version et sa data version ;
- items, item_aliases : objets vanilla (server_id NULL) et leurs noms en français et
  en anglais ; les noms propres à un serveur (argot) ne sont pas touchés ;
- recipes : les recettes vanilla de cette version sont remplacées (rechargement sans doublon),
  valables à partir de sa data version ;
- servers.mc_data_version : renseigné pour les serveurs de servers.toml sur cette version ;
- noms courants et argot de config/item_aliases.toml (langue « fr-courant ») : remplacés
  à chaque chargement, pour qu'un nom retiré du fichier disparaisse de la base.
"""
import argparse
import json
import tomllib
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from api.db import connect, migrate
from api.settings import SERVER_DIR, from_env
from db.load import read_servers, sync_config

MC_DIR = SERVER_DIR / "data" / "minecraft"
ALIASES_FILE = SERVER_DIR / "config" / "item_aliases.toml"
LANGS = ("fr", "en")
CUSTOM_LANG = "fr-courant"


def load_game_data(conn: psycopg.Connection, data: dict, servers: dict | None = None) -> dict:
    """Charge game_data.json ; renvoie le bilan {items, aliases, recipes}."""
    name, dv = data["version"]["name"], data["version"]["data_version"]
    with conn.transaction():
        conn.execute("""INSERT INTO mc_versions (data_version, name) VALUES (%s, %s)
                        ON CONFLICT (data_version) DO UPDATE SET name = EXCLUDED.name""", (dv, name))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO items (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                            [(i["id"],) for i in data["items"]])
            aliases = [(i["id"], lang, alias) for i in data["items"] for lang in LANGS for alias in i[lang]]
            cur.executemany("""INSERT INTO item_aliases (item_id, server_id, lang, alias)
                               VALUES (%s, NULL, %s, %s) ON CONFLICT DO NOTHING""", aliases)
            # Recettes vanilla de cette version : remplacées en bloc.
            cur.execute("DELETE FROM recipes WHERE server_id IS NULL AND lower(versions) = %s", (dv,))
            cur.executemany(
                """INSERT INTO recipes (result_item, result_qty, type, pattern, versions)
                   VALUES (%s, %s, %s, %s, %s)""",
                [(r["result"], r["count"], r["type"], Jsonb({"id": r["id"], **r["pattern"]}),
                  Range(dv, None, "[)")) for r in data["recipes"]])
        for slug, conf in (servers or {}).items():
            if conf.get("mc_version") == name:
                conn.execute("UPDATE servers SET mc_data_version = %s WHERE slug = %s", (dv, slug))
    return {"items": len(data["items"]), "aliases": len(aliases), "recipes": len(data["recipes"])}


def load_custom_aliases(conn: psycopg.Connection, config: dict) -> int:
    """Noms de config/item_aliases.toml ; [common] = tous serveurs, [<slug>] = un serveur."""
    rows = []
    with conn.transaction():
        conn.execute("DELETE FROM item_aliases WHERE lang = %s", (CUSTOM_LANG,))
        for section, entries in config.items():
            server_id = None
            if section != "common":
                row = conn.execute("SELECT id FROM servers WHERE slug = %s", (section,)).fetchone()
                if not row:
                    raise SystemExit(f"item_aliases.toml : serveur [{section}] absent de servers.toml")
                server_id = row[0]
            for item_id, aliases in entries.items():
                if item_id.startswith("minecraft:"):
                    if not conn.execute("SELECT 1 FROM items WHERE id = %s", (item_id,)).fetchone():
                        raise SystemExit(f"item_aliases.toml : objet inconnu {item_id} (faute de frappe ?)")
                else:   # objet propre au serveur
                    conn.execute("INSERT INTO items (id, server_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                                 (item_id, server_id))
                rows += [(item_id, server_id, CUSTOM_LANG, alias) for alias in aliases]
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO item_aliases (item_id, server_id, lang, alias)
                               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""", rows)
    return len(rows)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="26.2", help="version de Minecraft (dossier data/minecraft/<version>)")
    args = ap.parse_args(argv)

    path: Path = MC_DIR / args.version / "game_data.json"
    if not path.exists():
        raise SystemExit(f"{path} introuvable : lance d'abord python -m ingestion.minecraft_data "
                         f"--version {args.version}")
    settings = from_env()
    if not settings.database_url:
        raise SystemExit("ASSIST_DATABASE_URL n'est pas défini (voir server/.env.example)")
    data = json.loads(path.read_text("utf8"))
    servers = read_servers(settings.servers_file)
    with connect(settings.database_url) as conn:
        migrate(conn)
        sync_config(conn, servers)   # pour que servers existe avant d'y écrire la data version
        stats = load_game_data(conn, data, servers)
        custom = load_custom_aliases(conn, tomllib.loads(ALIASES_FILE.read_text("utf8")))
    print(f"Minecraft {data['version']['name']} (data version {data['version']['data_version']}) : "
          f"{stats['items']} objets, {stats['aliases']} noms, {stats['recipes']} recettes chargés ; "
          f"{custom} noms courants ({ALIASES_FILE.name})")


if __name__ == "__main__":
    main()
