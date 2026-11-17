"""Extraction des données de Minecraft vanilla (objets, noms, recettes) depuis les fichiers du jeu.

Lit uniquement des fichiers locaux, copiés depuis le .minecraft d'un PC de jeu par
collect_minecraft_files.ps1 : aucun appel réseau.

    server/data/minecraft/<version>/
        <version>.jar   recettes (data/minecraft/recipe/), tags d'objets, en_us.json, version.json
        fr_fr.json      noms français (absent du jar, rangé dans les assets du launcher)

Usage (depuis server/) :
    python -m ingestion.minecraft_data --version 26.2

Résultat : data/minecraft/<version>/game_data.json, chargé en base par
    python -m db.load_minecraft --version 26.2
"""
import argparse
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

from ingestion.common import DATA_DIR

RECIPE_DIR = "data/minecraft/recipe/"
TAG_DIR = "data/minecraft/tags/item/"
LANG_KINDS = ("item", "block")   # clés « item.minecraft.<nom> » et « block.minecraft.<nom> »


def item_names(lang: dict[str, str]) -> dict[str, list[str]]:
    """Noms d'objets d'un fichier de langue : {'minecraft:furnace': ['Four']}.

    Seules les clés à trois parties sont gardées : « item.minecraft.potion.effect.water »
    ou « …music_disc_cat.desc » sont des variantes ou des descriptions, pas des objets.
    """
    names: dict[str, list[str]] = {}
    for key, value in lang.items():
        parts = key.split(".")
        if len(parts) != 3 or parts[0] not in LANG_KINDS or parts[1] != "minecraft" or not value.strip():
            continue
        item_id = f"{parts[1]}:{parts[2]}"
        if value not in names.setdefault(item_id, []):
            names[item_id].append(value)
    return names


def read_tags(jar: zipfile.ZipFile) -> dict[str, list[str]]:
    """Tags d'objets, développés récursivement : {'#minecraft:planks': ['minecraft:oak_planks', …]}."""
    raw = {}
    for name in jar.namelist():
        if name.startswith(TAG_DIR) and name.endswith(".json"):
            tag = "#minecraft:" + name[len(TAG_DIR):-len(".json")]
            raw[tag] = [v if isinstance(v, str) else v["id"] for v in json.loads(jar.read(name))["values"]]

    resolved: dict[str, list[str]] = {}

    def expand(tag: str, seen: frozenset = frozenset()) -> list[str]:
        if tag in resolved:
            return resolved[tag]
        out: list[str] = []
        for value in raw.get(tag, []):
            if not value.startswith("#"):
                found = [value]
            elif value in seen:   # boucle entre tags : ignorée
                found = []
            else:
                found = expand(value, seen | {tag})
            out.extend(i for i in found if i not in out)
        resolved[tag] = out
        return out

    for tag in raw:
        expand(tag)
    return resolved


TAG_RE = re.compile(r"#[a-z0-9_.-]+:[a-z0-9_./-]+")
ITEM_RE = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+")


def _strings(value) -> list[str]:
    """Toutes les chaînes d'une valeur JSON (ingrédients, clés de grille, listes d'alternatives)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def _tags_used(value) -> set[str]:
    return {s for s in _strings(value) if TAG_RE.fullmatch(s)}


def _items_used(value) -> set[str]:
    """Identifiants d'objets cités ; les lignes de grille (« # # ») ne correspondent pas."""
    return {s for s in _strings(value) if ITEM_RE.fullmatch(s)}


def read_recipes(jar: zipfile.ZipFile, tags: dict[str, list[str]]) -> tuple[list[dict], list[str]]:
    """Recettes du jar. Renvoie (recettes, identifiants des recettes ignorées faute de résultat).

    Le détail propre à chaque type (grille, ingrédients, temps de cuisson…) est gardé tel
    quel dans « pattern », avec le contenu des tags qu'il cite (« tags »), pour que le
    prompt puisse dire « Pierre taillée, Roche noire ou Pierre des abîmes taillée ».
    """
    recipes, skipped = [], []
    for name in sorted(jar.namelist()):
        if not (name.startswith(RECIPE_DIR) and name.endswith(".json")):
            continue
        recipe_id = "minecraft:" + name[len(RECIPE_DIR):-len(".json")]
        data = json.loads(jar.read(name))
        result = data.get("result")
        if not isinstance(result, dict) or "id" not in result:   # garnitures d'armure, réparation
            skipped.append(recipe_id)
            continue
        pattern = {k: v for k, v in data.items() if k not in ("type", "result")}
        used = sorted(_tags_used(pattern))
        if used:
            pattern["tags"] = {t: tags.get(t, []) for t in used}
        recipes.append({
            "id": recipe_id,
            "type": data["type"].removeprefix("minecraft:"),
            "result": result["id"],
            "count": result.get("count", 1),
            "pattern": pattern,
        })
    return recipes, skipped


def extract(version_dir: Path, version: str) -> dict:
    jar_path = version_dir / f"{version}.jar"
    fr_path = version_dir / "fr_fr.json"
    for path in (jar_path, fr_path):
        if not path.exists():
            raise SystemExit(f"Introuvable : {path} (copie-le avec ingestion/collect_minecraft_files.ps1)")

    with zipfile.ZipFile(jar_path) as jar:
        info = json.loads(jar.read("version.json"))
        en = item_names(json.loads(jar.read("assets/minecraft/lang/en_us.json")))
        tags = read_tags(jar)
        recipes, skipped = read_recipes(jar, tags)
    fr = item_names(json.loads(fr_path.read_text("utf8")))

    ids = set(en) | set(fr)
    for r in recipes:   # un objet cité par une recette doit exister, même sans nom
        ids |= {r["result"]} | _items_used(r["pattern"])
    items = [{"id": i, "en": en.get(i, []), "fr": fr.get(i, [])} for i in sorted(ids)]

    return {
        "version": {"name": info.get("name", version), "data_version": info["world_version"]},
        "items": items,
        "recipes": recipes,
        "skipped_recipes": skipped,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="26.2", help="version de Minecraft (dossier data/minecraft/<version>)")
    args = ap.parse_args(argv)

    version_dir = DATA_DIR / "minecraft" / args.version
    data = extract(version_dir, args.version)
    out = version_dir / "game_data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf8")

    named_fr = sum(1 for i in data["items"] if i["fr"])
    types = Counter(r["type"] for r in data["recipes"])
    print(f"Minecraft {data['version']['name']} (data version {data['version']['data_version']})")
    print(f"  {len(data['items'])} objets et blocs, dont {named_fr} avec un nom français")
    print(f"  {len(data['recipes'])} recettes ; {len(data['skipped_recipes'])} ignorées (sans résultat) :")
    for t, n in types.most_common():
        print(f"    {n:4}  {t}")
    print(f"  -> {out}")


if __name__ == "__main__":
    main()
