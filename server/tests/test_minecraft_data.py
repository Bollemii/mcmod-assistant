"""Tests de l'extraction des données Minecraft, sur un faux jar construit en mémoire (sans réseau)."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ingestion.minecraft_data import extract, item_names

EN = {
    "block.minecraft.furnace": "Furnace",
    "item.minecraft.diamond": "Diamond",
    "block.minecraft.cobblestone": "Cobblestone",
    "item.minecraft.potion.effect.water": "Water Bottle",   # variante : ignorée
    "item.modifiers.chest": "When on Body:",                 # pas un objet
    "gui.done": "Done",
}
FR = {
    "block.minecraft.furnace": "Fourneau",
    "item.minecraft.diamond": "Diamant",
    "block.minecraft.cobblestone": "Pierre taillée",
}
FILES = {
    "version.json": {"name": "26.2", "world_version": 4903},
    "assets/minecraft/lang/en_us.json": EN,
    "data/minecraft/tags/item/stone_crafting_materials.json":
        {"values": ["minecraft:cobblestone", "#minecraft:deep_stones"]},
    "data/minecraft/tags/item/deep_stones.json": {"values": ["minecraft:cobbled_deepslate"]},
    "data/minecraft/recipe/furnace.json": {
        "type": "minecraft:crafting_shaped", "key": {"#": "#minecraft:stone_crafting_materials"},
        "pattern": ["###", "# #", "###"], "result": {"id": "minecraft:furnace"}},
    "data/minecraft/recipe/diamond_from_smelting.json": {
        "type": "minecraft:smelting", "ingredient": ["minecraft:diamond_ore", "minecraft:deepslate_diamond_ore"],
        "experience": 1.0, "result": {"id": "minecraft:diamond", "count": 2}},
    "data/minecraft/recipe/repair_item.json": {"type": "minecraft:crafting_special_repairitem"},
}


class ExtractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        with zipfile.ZipFile(folder / "26.2.jar", "w") as jar:
            for name, content in FILES.items():
                jar.writestr(name, json.dumps(content))
        (folder / "fr_fr.json").write_text(json.dumps(FR, ensure_ascii=False), "utf8")
        cls.data = extract(folder, "26.2")
        cls.items = {i["id"]: i for i in cls.data["items"]}
        cls.recipes = {r["id"]: r for r in cls.data["recipes"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_version(self):
        self.assertEqual(self.data["version"], {"name": "26.2", "data_version": 4903})

    def test_noms_en_et_fr(self):
        self.assertEqual(self.items["minecraft:furnace"], {"id": "minecraft:furnace", "en": ["Furnace"],
                                                            "fr": ["Fourneau"]})

    def test_cles_de_langue_ignorees(self):
        names = item_names(EN)
        self.assertNotIn("minecraft:potion", names)
        self.assertNotIn("modifiers:chest", names)
        self.assertEqual(set(names), {"minecraft:furnace", "minecraft:diamond", "minecraft:cobblestone"})

    def test_objets_cites_par_les_recettes_sans_nom(self):
        self.assertEqual(self.items["minecraft:deepslate_diamond_ore"]["fr"], [])
        self.assertIn("minecraft:diamond_ore", self.items)

    def test_tags_developpes_recursivement(self):
        furnace = self.recipes["minecraft:furnace"]
        self.assertEqual(furnace["type"], "crafting_shaped")
        self.assertEqual(furnace["pattern"]["tags"], {"#minecraft:stone_crafting_materials":
                                                      ["minecraft:cobblestone", "minecraft:cobbled_deepslate"]})
        # les lignes de grille « ### » ne sont ni des tags ni des objets
        self.assertNotIn("###", self.items)

    def test_quantite_et_alternatives(self):
        smelt = self.recipes["minecraft:diamond_from_smelting"]
        self.assertEqual((smelt["result"], smelt["count"]), ("minecraft:diamond", 2))
        self.assertNotIn("tags", smelt["pattern"])

    def test_recettes_sans_resultat_ignorees(self):
        self.assertEqual(self.data["skipped_recipes"], ["minecraft:repair_item"])


if __name__ == "__main__":
    unittest.main()
