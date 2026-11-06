"""Scénarios du système de confiance. Lancer : python -m unittest server/qa/test_trust.py"""
import unittest
from datetime import datetime, timedelta

from trust import (Author, Claim, author_trust, group_confidences, next_status,
                   own_confidence, reputation_deltas)

ROLES = {"moderateur": 0.9, "guide": 0.85, "joueur": 0.3}
NOW = datetime(2026, 11, 5)


def claims(*authors, stance=1, conf=0.9):
    """Une occurrence par auteur ; un auteur = (id, rôle)."""
    return [Claim(a, stance, author_trust(Author(ROLES[r])), conf) for a, r in authors]


def players(n, prefix="p"):
    return [(f"{prefix}{i}", "joueur") for i in range(n)]


def conf_status(cs, official="consistent", **kw):
    c = own_confidence(cs, **kw)
    return round(c, 2), next_status("candidate", c, official)[0]


class AuthorTrust(unittest.TestCase):
    def test_role_is_the_prior(self):
        self.assertAlmostEqual(author_trust(Author(0.9)), 0.9)

    def test_history_moves_trust(self):
        self.assertGreater(author_trust(Author(0.3, confirmed=8)), 0.7)
        self.assertLess(author_trust(Author(0.85, contradicted=6)), 0.4)

    def test_banned_and_cap(self):
        self.assertEqual(author_trust(Author(0.9, banned=True)), 0)
        self.assertLessEqual(author_trust(Author(0.95, confirmed=100)), 0.95)


class FactConfidence(unittest.TestCase):
    def test_one_moderator_is_enough(self):
        self.assertEqual(conf_status(claims(("m", "moderateur"))), (0.81, "active"))

    def test_one_guide_is_not(self):
        self.assertEqual(conf_status(claims(("g", "guide")))[1], "candidate")

    def test_guide_plus_one_player(self):
        self.assertEqual(conf_status(claims(("g", "guide"), ("p", "joueur")))[1], "active")

    def test_players_need_numbers(self):
        self.assertEqual(conf_status(claims(*players(4)))[1], "candidate")
        self.assertEqual(conf_status(claims(*players(6)))[1], "active")

    def test_repeating_does_not_count(self):
        spam = claims(("p", "joueur")) * 20
        self.assertEqual(conf_status(spam), conf_status(claims(("p", "joueur"))))

    def test_contradiction_by_moderator(self):
        cs = claims(*players(6)) + claims(("m", "moderateur"), stance=-1)
        self.assertEqual(conf_status(cs)[1], "candidate")

    def test_negative_feedback(self):
        cs = claims(("m", "moderateur"))
        self.assertEqual(conf_status(cs, feedback_down=3)[1], "candidate")

    def test_decay(self):
        cs = claims(("m", "moderateur"))
        c = own_confidence(cs, last_seen=NOW - timedelta(days=60), now=NOW)
        self.assertAlmostEqual(c, own_confidence(cs) / 2, places=3)

    def test_needs_official_check_before_activation(self):
        c = own_confidence(claims(("m", "moderateur")))
        self.assertEqual(next_status("candidate", c, "unchecked"), ("candidate", True))
        self.assertEqual(next_status("candidate", c, "contradicts")[0], "rejected")


class Competition(unittest.TestCase):
    def test_lone_player_does_not_beat_moderator(self):
        own = {1: own_confidence(claims(("m", "moderateur"))), 2: own_confidence(claims(("p", "joueur")))}
        final = group_confidences(own)
        self.assertEqual(next_status("candidate", final[1], "consistent")[0], "active")
        self.assertEqual(next_status("candidate", final[2], "consistent")[0], "candidate")

    def test_comparable_answers_are_both_contested(self):
        own = {1: own_confidence(claims(*players(6, "a"))), 2: own_confidence(claims(*players(5, "b")))}
        final = group_confidences(own)
        self.assertTrue(all(next_status("candidate", c, "consistent")[0] == "candidate"
                            for c in final.values()))


class Lifecycle(unittest.TestCase):
    def test_hysteresis(self):
        self.assertEqual(next_status("active", 0.7, "consistent")[0], "active")
        self.assertEqual(next_status("candidate", 0.7, "consistent")[0], "candidate")
        self.assertEqual(next_status("active", 0.5, "consistent")[0], "candidate")
        self.assertEqual(next_status("active", 0.2, "consistent")[0], "expired")

    def test_human_decisions_and_season(self):
        self.assertEqual(next_status("pinned", 0.0, "contradicts")[0], "pinned")
        self.assertEqual(next_status("active", 0.9, "consistent", season_ok=False)[0], "expired")

    def test_reputation(self):
        cs = claims(("a", "joueur"), ("b", "joueur")) + claims(("c", "joueur"), stance=-1)
        self.assertEqual(reputation_deltas(cs, "active"), {"a": (1, 0), "b": (1, 0), "c": (0, 1)})
        self.assertEqual(reputation_deltas(cs, "candidate"), {})


if __name__ == "__main__":
    unittest.main()
