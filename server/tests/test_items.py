"""Tests de la détection des objets dans la question et de la mise en forme des recettes.

Sans base ni réseau : l'index est construit en mémoire. La deuxième série utilise les vraies
données de 26.2 (data/minecraft/26.2/game_data.json, produit par ingestion.minecraft_data)
et est ignorée si le fichier est absent.
"""
import json
import tomllib
import unittest

from api.items import ItemIndex, Recipe, normalize, recipe_context, token_score, tokens
from api.llm import format_extract
from api.settings import SERVER_DIR

GAME_DATA = SERVER_DIR / "data" / "minecraft" / "26.2" / "game_data.json"
ALIASES = SERVER_DIR / "config" / "item_aliases.toml"


def build_index(items: list[dict], recipes: list[dict], custom: dict | None = None) -> ItemIndex:
    """Même construction que api.items.load_index, à partir de game_data.json."""
    index = ItemIndex()
    for i in items:
        for lang in ("fr", "en"):
            for alias in i[lang]:
                if lang == "fr":
                    index.names.setdefault(i["id"], alias)
                index.add_alias(i["id"], alias)
    for section, entries in (custom or {}).items():
        for item_id, aliases in entries.items():
            for alias in aliases:
                index.add_alias(item_id, alias, None if section == "common" else section)
    for n, r in enumerate(recipes, 1):
        index.recipes[r["result"]].append(Recipe(n, r["type"], r["result"], r["count"],
                                                 {"id": r["id"], **r["pattern"]}))
    return index


SMALL_ITEMS = [
    {"id": "minecraft:enchanting_table", "fr": ["Table d'enchantement"], "en": ["Enchanting Table"]},
    {"id": "minecraft:furnace", "fr": ["Fourneau"], "en": ["Furnace"]},
    {"id": "minecraft:diamond", "fr": ["Diamant"], "en": ["Diamond"]},
    {"id": "minecraft:diamond_pickaxe", "fr": ["Pioche en diamant"], "en": ["Diamond Pickaxe"]},
    {"id": "minecraft:paper", "fr": ["Papier"], "en": ["Paper"]},
    {"id": "minecraft:crafter", "fr": ["Fabricateur"], "en": ["Crafter"]},
    {"id": "minecraft:obsidian", "fr": ["Obsidienne"], "en": ["Obsidian"]},
    {"id": "minecraft:book", "fr": ["Livre"], "en": ["Book"]},
    {"id": "minecraft:cobblestone", "fr": ["Pierres"], "en": ["Cobblestone"]},
    {"id": "minecraft:glass", "fr": ["Verre"], "en": ["Glass"]},
    {"id": "minecraft:sand", "fr": ["Sable"], "en": ["Sand"]},
    {"id": "minecraft:horse_spawn_egg", "fr": ["Œuf d'apparition de cheval"], "en": ["Horse Spawn Egg"]},
]
SMALL_RECIPES = [
    {"id": "minecraft:enchanting_table", "type": "crafting_shaped", "result": "minecraft:enchanting_table",
     "count": 1, "pattern": {"key": {"#": "minecraft:obsidian", "B": "minecraft:book", "D": "minecraft:diamond"},
                             "pattern": [" B ", "D#D", "###"]}},
    {"id": "minecraft:furnace", "type": "crafting_shaped", "result": "minecraft:furnace", "count": 1,
     "pattern": {"key": {"#": "#minecraft:stone_crafting_materials"}, "pattern": ["###", "# #", "###"],
                 "tags": {"#minecraft:stone_crafting_materials": ["minecraft:cobblestone"]}}},
    {"id": "minecraft:glass", "type": "smelting", "result": "minecraft:glass", "count": 1,
     "pattern": {"ingredient": "minecraft:sand", "experience": 0.1}},
    {"id": "minecraft:diamond_pickaxe", "type": "crafting_shaped", "result": "minecraft:diamond_pickaxe",
     "count": 1, "pattern": {"key": {"#": "minecraft:stick", "X": "minecraft:diamond"},
                             "pattern": ["XXX", " # ", " # "]}},
]
SMALL_CUSTOM = {"common": {"minecraft:furnace": ["four"]},
                "excalia": {"excalia:four_nucleaire": ["four nucléaire"]}}


def ids(index: ItemIndex, question: str, server: str | None = None) -> list[str]:
    return [m.item_ids[0] for m in index.detect(question, server)]


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("Table d'Enchantement !"), "table d enchantement")
        self.assertEqual(normalize("Œuf d'apparition"), "oeuf d apparition")

    def test_tokens_sans_petits_mots_et_au_singulier(self):
        self.assertEqual(tokens("des planches de chêne"), ["planche", "chene"])
        self.assertEqual(tokens("chevaux"), ["cheval"])

    def test_token_score(self):
        self.assertEqual(token_score("diamant", "diamant"), 2)
        self.assertEqual(token_score("enchant", "enchantement"), 1)      # abréviation
        self.assertEqual(token_score("enchentement", "enchantement"), 1)  # faute de frappe
        self.assertEqual(token_score("palier", "papier", typos=False), 0)
        self.assertEqual(token_score("ench", "enchantement"), 0)          # abréviation trop courte


class DetectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = build_index(SMALL_ITEMS, SMALL_RECIPES, SMALL_CUSTOM)

    def test_nom_officiel_abrege_et_mal_ecrit(self):
        for q in ("comment craft une table d'enchant ?", "Table d'enchantement", "table enchantement",
                  "comment faire une table d'enchentement", "enchanting table recipe"):
            self.assertEqual(ids(self.index, q), ["minecraft:enchanting_table"], q)

    def test_nom_courant(self):
        self.assertEqual(ids(self.index, "Comment craft un four ?"), ["minecraft:furnace"])

    def test_craft_n_est_pas_le_crafter(self):
        self.assertEqual(ids(self.index, "craft"), [])
        self.assertEqual(ids(self.index, "comment faire un fabricateur"), ["minecraft:crafter"])

    def test_pas_de_faute_de_frappe_sur_un_nom_d_un_mot(self):
        self.assertEqual(ids(self.index, "comment monter de palier"), [])

    def test_le_nom_le_plus_long_l_emporte(self):
        self.assertEqual(ids(self.index, "recette de la pioche en diams"), ["minecraft:diamond_pickaxe"])

    def test_plusieurs_objets_dans_l_ordre(self):
        self.assertEqual(ids(self.index, "diamant ou obsidienne ?"), ["minecraft:diamond", "minecraft:obsidian"])

    def test_objet_du_serveur_masque_le_nom_vanilla(self):
        self.assertEqual(ids(self.index, "comment faire un four nucléaire", "excalia"), ["excalia:four_nucleaire"])
        # sur un autre serveur, le nom d'Excalia n'existe pas
        self.assertEqual(ids(self.index, "comment faire un four nucléaire", "autre"), ["minecraft:furnace"])

    def test_question_sans_objet(self):
        self.assertEqual(ids(self.index, "comment créer mon île ?"), [])


class RecipeContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = build_index(SMALL_ITEMS, SMALL_RECIPES, SMALL_CUSTOM)

    def context(self, question, server_items=None):
        return recipe_context(self.index, self.index.detect(question), "Excalia", server_items)

    def test_recette_avec_forme(self):
        [c] = self.context("table d'enchant")
        self.assertEqual(c.kind, "game_data")
        self.assertEqual(c.url, "")
        self.assertEqual(c.recipe_ids, [1])
        self.assertIn("grille : · B · / D # D / # # #", c.content)
        self.assertIn("# = Obsidienne; B = Livre; D = Diamant", c.content)
        self.assertIn("peut différer sur le serveur", c.content)
        self.assertTrue(format_extract(1, c).startswith("[1] Données du jeu — Minecraft vanilla > Table"))

    def test_tag_developpe(self):
        [c] = self.context("four")
        self.assertIn("# = Pierres", c.content)

    def test_cuisson_avec_duree_par_defaut(self):
        [c] = self.context("verre")
        self.assertIn("cuisson au fourneau -> Verre", c.content)
        self.assertIn("à partir de : Sable, 10 s, 0,1 XP", c.content)

    def test_objet_sans_recette_ignore(self):
        self.assertEqual(self.context("papier"), [])

    def test_note_du_serveur(self):
        [c] = self.context("table d'enchant", {"notes": {"minecraft:enchanting_table": "rien ne s'enchante."}})
        self.assertTrue(c.content.startswith("Sur Excalia : rien ne s'enchante."))
        self.assertIn("grille", c.content)

    def test_objet_desactive(self):
        [c] = self.context("four", {"disabled": ["minecraft:furnace"]})
        self.assertIn("désactivé", c.content)
        self.assertNotIn("grille", c.content)
        self.assertEqual(c.recipe_ids, [])

    def test_au_plus_deux_objets(self):
        self.assertEqual(len(self.context("table d'enchant, four, verre et pioche en diamant")), 2)


@unittest.skipUnless(GAME_DATA.exists(), "game_data.json absent (python -m ingestion.minecraft_data)")
class RealDataTest(unittest.TestCase):
    """Questions de joueurs sur les vraies données de Minecraft 26.2."""

    @classmethod
    def setUpClass(cls):
        data = json.loads(GAME_DATA.read_text("utf8"))
        custom = tomllib.loads(ALIASES.read_text("utf8"))
        cls.index = build_index(data["items"], data["recipes"], custom)

    def test_questions(self):
        cases = {
            "comment craft une table d'enchant ?": "minecraft:enchanting_table",
            "Comment craft un four ?": "minecraft:furnace",
            "recette de la pioche en diams": "minecraft:diamond_pickaxe",
            "comment faire des planches de chene": "minecraft:oak_planks",
            "comment faire un etabli": "minecraft:crafting_table",
            "comment faire une table de craft": "minecraft:crafting_table",
            "craft hopper": "minecraft:hopper",
            "comment faire des torches": "minecraft:torch",
            "comment faire une pomme d'or": "minecraft:golden_apple",
            "comment faire un lit": "minecraft:white_bed",
            "comment faire des fusées de feux d'artifice": "minecraft:firework_rocket",
            "comment faire un piston collant": "minecraft:sticky_piston",
            "comment faire une table d'enchentement": "minecraft:enchanting_table",
            "comment faire une épée en netherite": "minecraft:netherite_sword",
            "comment faire de la poudre d'os": "minecraft:bone_meal",
            "comment fabriquer un seau": "minecraft:bucket",
        }
        for question, expected in cases.items():
            self.assertEqual(ids(self.index, question)[:1], [expected], question)

    def test_questions_sans_objet(self):
        for q in ("comment créer mon île ?", "c'est quoi le broyeur", "combien coute le grade VIP",
                  "comment monter de palier", "comment avoir des excacoins", "comment crafter"):
            self.assertEqual(recipe_context(self.index, self.index.detect(q, "excalia")), [], q)

    def test_tous_les_noms_courants_existent(self):
        custom = tomllib.loads(ALIASES.read_text("utf8"))
        known = {i["id"] for i in json.loads(GAME_DATA.read_text("utf8"))["items"]}
        for item_id in custom.get("common", {}):
            self.assertIn(item_id, known)


if __name__ == "__main__":
    unittest.main()
