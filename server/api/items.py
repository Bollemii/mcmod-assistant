"""Détection des objets Minecraft cités dans une question, et recettes à donner au LLM.

« comment craft une table d'enchant ? » -> minecraft:enchanting_table -> sa recette vanilla,
mise en forme comme un extrait « Données du jeu » placé avant les extraits du wiki.

Détection : les noms des objets (item_aliases : fr_fr.json, en_us.json, noms courants de
config/item_aliases.toml, argot propre au serveur) sont cherchés dans la question, mot à
mot, en tolérant :
- les accents, majuscules, apostrophes et traits d'union (« Établi » = « etabli ») ;
- les petits mots (« table enchantement » = « table d'enchantement ») ;
- le pluriel (« planches » = « planche », « chevaux » = « cheval ») ;
- les abréviations par début de mot (« enchant » = « enchantement », « diams » = « diamant ») ;
- une faute de frappe par mot (deux pour un mot long), seulement dans un nom de plusieurs
  mots dont au moins un est exact : « palier » ne doit pas devenir « papier ».
Le nom le plus long l'emporte : « pioche en diamant » donne la pioche, pas le diamant.

Priorité au serveur ([<serveur>.items] dans servers.toml) : un objet désactivé n'a pas
de recette vanilla ; une note du serveur est placée devant la recette.

L'index tient en mémoire (~3 500 noms, ~1 600 recettes) ; il est lu en base au premier
besoin, puis relu au redémarrage de l'API (après db.load_minecraft).
"""
import re
import threading
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

import psycopg

from .retrieval import ContextChunk

# Mots ignorés des deux côtés : « table d'enchantement » = « table enchantement ».
STOPWORDS = {"de", "d", "du", "des", "en", "la", "le", "les", "l", "a", "au", "aux", "un", "une",
             "of", "the", "and"}
# Noms d'objets qui sont aussi des mots courants des questions : jamais détectés.
# « craft » est le verbe des joueurs, pas l'objet Crafter (« Fabricateur » reste détecté).
IGNORED_ALIASES = {"crafter"}
MAX_ITEMS = 2              # objets dont on donne les recettes
MAX_RECIPES_PER_ITEM = 3
MAX_TAG_ITEMS = 4          # « Planches de chêne, de sapin, … (11 au choix) »
TYPE_ORDER = ["crafting_shaped", "crafting_shapeless", "smelting", "blasting", "smoking",
              "campfire_cooking", "smithing_transform", "stonecutting", "crafting_transmute"]
GAME_DATA = "game_data"


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation : « Table d'Enchantement » -> « table d enchantement »."""
    text = text.lower().replace("œ", "oe").replace("æ", "ae")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def singular(token: str) -> str:
    if len(token) > 4 and token.endswith("aux"):
        return token[:-3] + "al"
    if len(token) > 3 and token[-1] in "sx":
        return token[:-1]
    return token


def tokens(text: str) -> list[str]:
    return [singular(t) for t in normalize(text).split() if t not in STOPWORDS]


def _distance(a: str, b: str, limit: int) -> int:
    """Distance d'édition (Levenshtein), arrêtée dès qu'elle dépasse limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def token_score(q: str, a: str, typos: bool = True) -> int:
    """Correspondance d'un mot de la question (q) avec un mot d'un nom (a) : 2 exact, 1 approché, 0 non."""
    if q == a:
        return 2
    if len(q) >= 4 and a.startswith(q) and len(q) * 2 >= len(a):      # abréviation
        return 1
    if typos and len(a) >= 5:
        limit = 2 if len(a) >= 9 else 1
        if _distance(q, a, limit) <= limit:                            # faute de frappe
            return 1
    return 0


@dataclass
class Match:
    item_ids: list[str]        # plusieurs si le nom est ambigu (« Disque »)
    alias: str
    start: int
    end: int
    exact: int                 # nombre de mots identiques (départage)
    server: bool = False       # nom propre au serveur (argot, objet du serveur)


@dataclass
class Recipe:
    id: int
    type: str
    result: str
    count: int
    pattern: dict


@dataclass
class ItemIndex:
    names: dict[str, str] = field(default_factory=dict)                  # id -> nom français officiel
    # (serveur ou None, mots du nom) -> (nom affiché, objets)
    aliases: dict[tuple, tuple[str, list[str]]] = field(default_factory=dict)
    by_first: dict[str, list[tuple]] = field(default_factory=lambda: defaultdict(list))
    recipes: dict[str, list[Recipe]] = field(default_factory=lambda: defaultdict(list))

    def add_alias(self, item_id: str, alias: str, server: str | None = None) -> None:
        toks = tuple(tokens(alias))
        if not toks or " ".join(toks) in IGNORED_ALIASES:
            return
        key = (server, toks)
        if key not in self.aliases:
            self.aliases[key] = (alias, [])
            self.by_first[toks[0]].append(key)
        if item_id not in self.aliases[key][1]:
            self.aliases[key][1].append(item_id)

    def detect(self, question: str, server: str | None = None) -> list[Match]:
        """Objets cités, dans l'ordre de la question, sans chevauchement (le nom le plus long gagne)."""
        q = tokens(question)
        found = []
        for i, word in enumerate(q):
            for first, keys in self.by_first.items():
                if not token_score(word, first):
                    continue
                for key in keys:
                    alias_server, toks = key
                    if alias_server not in (None, server) or i + len(toks) > len(q):
                        continue
                    typos = len(toks) > 1
                    scores = [token_score(qw, aw, typos) for qw, aw in zip(q[i:i + len(toks)], toks)]
                    if all(scores) and (2 in scores or len(toks) == 1):
                        name, items = self.aliases[key]
                        found.append(Match(items, name, i, i + len(toks), scores.count(2),
                                           alias_server is not None))
        # Plus long d'abord, puis le plus exact ; un nom du serveur passe devant un nom commun.
        found.sort(key=lambda m: (-(m.end - m.start), -m.exact, not m.server, m.start))
        taken, chosen = set(), []
        for m in found:
            span = set(range(m.start, m.end))
            if not span & taken:
                taken |= span
                chosen.append(m)
        return sorted(chosen, key=lambda m: m.start)


def load_index(conn: psycopg.Connection) -> ItemIndex:
    index = ItemIndex()
    rows = conn.execute("""SELECT a.item_id, a.lang, a.alias, s.slug FROM item_aliases a
                           LEFT JOIN servers s ON s.id = a.server_id ORDER BY a.id""").fetchall()
    for item_id, lang, alias, slug in rows:
        if lang == "fr" and slug is None:
            index.names.setdefault(item_id, alias)
        index.add_alias(item_id, alias, slug)
    for rid, rtype, result, count, pattern in conn.execute(
            """SELECT id, type, result_item, result_qty, pattern FROM recipes
               WHERE server_id IS NULL AND NOT disabled ORDER BY id"""):
        index.recipes[result].append(Recipe(rid, rtype, result, count, pattern))
    return index


class ItemIndexCache:
    """Index lu en base au premier besoin ; s'il est vide (données pas encore chargées), relu
    au plus une fois par minute."""

    def __init__(self, retry_s: float = 60):
        self.index: ItemIndex | None = None
        self.loaded_at = 0.0
        self.retry_s = retry_s
        self.lock = threading.Lock()

    def get(self, conn: psycopg.Connection) -> ItemIndex:
        with self.lock:
            if self.index is None or (not self.index.aliases and time.monotonic() - self.loaded_at > self.retry_s):
                self.index = load_index(conn)
                self.loaded_at = time.monotonic()
            return self.index


# --- Mise en forme des recettes ---------------------------------------------------------

TYPE_LABELS = {
    "crafting_shaped": "établi, avec forme",
    "crafting_shapeless": "établi, sans forme (placement libre)",
    "smelting": "cuisson au fourneau",
    "blasting": "cuisson au haut fourneau",
    "smoking": "cuisson au fumoir",
    "campfire_cooking": "cuisson sur un feu de camp",
    "stonecutting": "tailleur de pierre",
    "smithing_transform": "table de forge",
    "crafting_transmute": "établi, transformation",
}


# Durée de cuisson par défaut du jeu, en ticks (20 par seconde), quand la recette n'en donne pas.
COOKING_TICKS = {"smelting": 200, "blasting": 100, "smoking": 100, "campfire_cooking": 600}


def _name(index: ItemIndex, item_id: str) -> str:
    return index.names.get(item_id, item_id.removeprefix("minecraft:"))


def _ingredient(index: ItemIndex, value, tags: dict) -> str:
    """'minecraft:stick' -> 'Bâton' ; '#minecraft:planks' -> 'Planches de chêne, … (11 au choix)'."""
    if isinstance(value, list):
        return _choices([_ingredient(index, v, tags) for v in value])
    if isinstance(value, dict):          # ancien format {"item": …} / {"tag": …}
        value = value.get("item") or ("#" + value["tag"] if "tag" in value else str(value))
    if isinstance(value, str) and value.startswith("#"):
        members = tags.get(value, [])
        if not members:
            return value
        return _choices([_name(index, m) for m in members])
    return _name(index, str(value))


def _choices(names: list[str]) -> str:
    """['A', 'B'] -> 'A ou B' ; au-delà de MAX_TAG_ITEMS : 'A, B, C, D, … (12 au choix)'."""
    if len(names) <= MAX_TAG_ITEMS:
        return " ou ".join(names)
    return ", ".join(names[:MAX_TAG_ITEMS]) + f", … ({len(names)} au choix)"


def _num(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def format_recipe(index: ItemIndex, r: Recipe) -> str:
    p, tags = r.pattern, r.pattern.get("tags", {})
    qty = f"{r.count} × " if r.count > 1 else ""
    head = f"- {TYPE_LABELS.get(r.type, r.type)} -> {qty}{_name(index, r.result)}"
    if r.type == "crafting_shaped":
        grid = [" ".join(ch if ch != " " else "·" for ch in row) for row in p.get("pattern", [])]
        legend = "; ".join(f"{k} = {_ingredient(index, v, tags)}" for k, v in p.get("key", {}).items())
        return f"{head}\n  grille : {' / '.join(grid)} (· = case vide)\n  avec {legend}"
    if r.type == "crafting_shapeless":
        counts: dict[str, int] = {}
        for ing in p.get("ingredients", []):
            label = _ingredient(index, ing, tags)
            counts[label] = counts.get(label, 0) + 1
        return f"{head}\n  ingrédients : " + ", ".join(f"{n} × {k}" if n > 1 else k for k, n in counts.items())
    if r.type in COOKING_TICKS:
        seconds = p.get("cookingtime", COOKING_TICKS[r.type]) / 20
        xp = f", {_num(p['experience'])} XP" if p.get("experience") else ""
        return f"{head}\n  à partir de : {_ingredient(index, p.get('ingredient'), tags)}, {_num(seconds)} s{xp}"
    if r.type == "stonecutting":
        return f"{head}\n  à partir de : 1 × {_ingredient(index, p.get('ingredient'), tags)}"
    if r.type == "smithing_transform":
        parts = [f"{label} : {_ingredient(index, p[k], tags)}"
                 for k, label in (("template", "modèle"), ("base", "objet"), ("addition", "ajout")) if k in p]
        return f"{head}\n  " + " ; ".join(parts)
    # Autres types (transformation, recettes spéciales) : les ingrédients tels quels.
    parts = [f"{k} : {_ingredient(index, v, tags)}" for k, v in p.items()
             if k not in ("id", "tags", "group", "category", "show_notification")]
    return f"{head}\n  " + " ; ".join(parts)


def recipe_context(index: ItemIndex, matches: list[Match], server_name: str | None = None,
                   server_items: dict | None = None) -> list[ContextChunk]:
    """Un extrait « Données du jeu » par objet détecté qui a une recette (MAX_ITEMS au plus)."""
    server_items = server_items or {}
    disabled = set(server_items.get("disabled", []))
    notes = server_items.get("notes", {})
    out, seen = [], set()
    for m in matches:
        for item_id in m.item_ids:
            if item_id in seen or len(out) >= MAX_ITEMS:
                continue
            recipes = sorted(index.recipes.get(item_id, []),
                             key=lambda r: TYPE_ORDER.index(r.type) if r.type in TYPE_ORDER else len(TYPE_ORDER))
            if not recipes and item_id not in disabled and item_id not in notes:
                continue
            seen.add(item_id)
            name = _name(index, item_id)
            lines = []
            where = server_name or "ce serveur"
            if item_id in disabled:
                lines.append(f"Sur {where}, cet objet est désactivé : la recette vanilla ne s'y applique pas.")
                recipes = []
            if item_id in notes:
                lines.append(f"Sur {where} : {notes[item_id]}")
            if recipes:
                lines.append(f"Recette{'s' if len(recipes) > 1 else ''} de Minecraft vanilla "
                             f"(peut différer sur le serveur) :")
                lines.extend(format_recipe(index, r) for r in recipes[:MAX_RECIPES_PER_ITEM])
                if len(recipes) > MAX_RECIPES_PER_ITEM:
                    lines.append(f"(et {len(recipes) - MAX_RECIPES_PER_ITEM} autres recettes)")
            out.append(ContextChunk(
                id=recipes[0].id if recipes else 0, url="", title=f"Recette : {name}",
                heading_path=["Minecraft vanilla", name], content="\n".join(lines), kind=GAME_DATA,
                published_at=None, key=f"recipe:{item_id}",
                recipe_ids=[r.id for r in recipes[:MAX_RECIPES_PER_ITEM]]))
    return out
