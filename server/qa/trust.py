"""Système de confiance des Q/R extraites du chat (voir docs/confiance-chat.md).

Fonctions pures, sans base de données : la tâche de maintenance lit les faits et leurs
occurrences, appelle ces fonctions, puis écrit confiance et statut.

Vocabulaire :
- claim : une occurrence dans le chat (tel auteur a donné, ou contredit, tel fait) ;
- fait (fact) : une question canonique et une réponse canonique ; plusieurs faits
  peuvent répondre à la même question (groupe) et se concurrencer.
"""
from dataclasses import dataclass
from datetime import datetime

# --- Paramètres (valeurs par défaut, ajustables) -------------------------------------
PRIOR_STRENGTH = 4        # poids du rôle face à l'historique : 4 réponses « valent » le rôle
MAX_TRUST = 0.95          # personne n'est infaillible
FEEDBACK_WEIGHT = 0.15    # poids d'un 👍 / 👎 de joueur sur une réponse qui a utilisé le fait
HALF_LIFE_DAYS = 60       # la confiance est divisée par 2 tous les 60 jours sans confirmation
ACTIVATE_AT = 0.80        # seuil pour qu'un fait devienne actif, sans validation humaine
DEACTIVATE_AT = 0.60      # un fait actif ne redescend qu'en dessous (hystérésis)
EXPIRE_AT = 0.30          # en dessous, le fait expire


@dataclass
class Author:
    base_trust: float      # confiance du rôle (chat_roles.base_trust)
    confirmed: int = 0
    contradicted: int = 0
    banned: bool = False


@dataclass
class Claim:
    author_id: str
    stance: int            # 1 soutient, -1 contredit
    trust: float           # author_trust() de l'auteur
    extraction_conf: float  # certitude du LLM qui a extrait la Q/R du chat


def author_trust(a: Author) -> float:
    """Confiance du rôle, corrigée par l'historique de l'auteur (moyenne bayésienne).

    Le rôle sert d'a priori, équivalent à PRIOR_STRENGTH réponses. Un guide souvent
    contredit perd de la confiance ; un joueur souvent confirmé en gagne.
    """
    if a.banned:
        return 0.0
    t = (a.base_trust * PRIOR_STRENGTH + a.confirmed) / (PRIOR_STRENGTH + a.confirmed + a.contradicted)
    return min(t, MAX_TRUST)


def noisy_or(weights: list[float]) -> float:
    """Probabilité qu'au moins une des sources indépendantes ait raison."""
    p = 1.0
    for w in weights:
        p *= 1 - w
    return 1 - p


def own_confidence(claims: list[Claim], feedback_up: int = 0, feedback_down: int = 0,
                   last_seen: datetime | None = None, now: datetime | None = None) -> float:
    """Confiance d'un fait seul, avant concurrence avec les autres réponses.

    - Un auteur ne compte qu'une fois par position (répéter 10 fois ne sert à rien).
    - Soutiens et contradictions sont combinés en « au moins un a raison » (noisy-OR).
    - La confiance décroît avec le temps écoulé depuis la dernière confirmation.
    """
    best: dict[tuple[str, int], float] = {}
    for c in claims:
        key = (c.author_id, c.stance)
        best[key] = max(best.get(key, 0.0), c.trust * c.extraction_conf)
    support = noisy_or([w for (_, s), w in best.items() if s == 1] + [FEEDBACK_WEIGHT] * feedback_up)
    against = noisy_or([w for (_, s), w in best.items() if s == -1] + [FEEDBACK_WEIGHT] * feedback_down)
    conf = support * (1 - against)
    if last_seen and now:
        conf *= 0.5 ** (max((now - last_seen).days, 0) / HALF_LIFE_DAYS)
    return conf


def group_confidences(own: dict[int, float]) -> dict[int, float]:
    """Confiance finale de faits concurrents (même question, réponses différentes).

    Un concurrent ne pénalise un fait que s'il atteint au moins la moitié de sa
    confiance : une réponse isolée d'un joueur ne fait pas tomber celle d'un modérateur,
    mais deux réponses de force comparable restent toutes deux « contestées ».
    """
    out = {}
    for fid, conf in own.items():
        rival = max((c for f, c in own.items() if f != fid), default=0.0)
        out[fid] = conf * (1 - max(0.0, rival - conf / 2))
    return out


def next_status(status: str, confidence: float, official: str, season_ok: bool = True) -> tuple[str, bool]:
    """Nouveau statut d'un fait, et s'il faut lancer la vérification par les sources officielles.

    official : 'unchecked', 'consistent', 'not_covered' ou 'contradicts' (résultat de la
    comparaison, par le LLM, du fait avec le wiki et les notes de patch).
    """
    if status in ("pinned", "banned"):          # décision humaine : on n'y touche pas
        return status, False
    if not season_ok:
        return "expired", False
    if official == "contradicts":
        return "rejected", False
    if confidence < EXPIRE_AT:
        return ("expired" if status in ("active", "expired") else "candidate"), False
    if status == "active":
        return ("active" if confidence >= DEACTIVATE_AT else "candidate"), False
    if confidence >= ACTIVATE_AT:
        if official == "unchecked":
            return "candidate", True               # assez de confiance : vérifier avant d'activer
        return "active", False
    return "candidate", False


def reputation_deltas(claims: list[Claim], new_status: str) -> dict[str, tuple[int, int]]:
    """(confirmed, contradicted) à ajouter à chaque auteur quand un fait est tranché."""
    if new_status not in ("active", "rejected"):
        return {}
    right = 1 if new_status == "active" else -1
    deltas = {}
    for c in {(c.author_id, c.stance) for c in claims}:
        author, stance = c
        deltas[author] = (1, 0) if stance == right else (0, 1)
    return deltas
