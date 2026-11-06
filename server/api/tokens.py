"""Jetons d'accès des joueurs.

Le fichier (server/config/tokens.txt par défaut) ne contient que l'empreinte SHA-256
de chaque jeton, suivie d'un nom libre :

    3f2a…e9  Emilien

Créer un jeton (affiché une seule fois) :
    python -m api.tokens new "Emilien"
Lister les jetons :
    python -m api.tokens list
"""
import hashlib
import secrets
import sys
from pathlib import Path

from .settings import from_env


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load(path: Path) -> dict[str, str]:
    """empreinte -> nom du joueur."""
    tokens = {}
    if path.exists():
        for line in path.read_text("utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                h, _, name = line.partition(" ")
                tokens[h] = name.strip() or "?"
    return tokens


def create(path: Path, name: str) -> str:
    token = "mca_" + secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf8") as f:
        f.write(f"{digest(token)}  {name}\n")
    return token


def main(argv: list[str]) -> None:
    path = from_env().tokens_file
    if len(argv) >= 2 and argv[0] == "new":
        token = create(path, " ".join(argv[1:]))
        print(f"Jeton créé pour {' '.join(argv[1:])} (à copier maintenant, il ne sera plus affiché) :")
        print(token)
    elif argv[:1] == ["list"]:
        for h, name in load(path).items():
            print(f"{h[:12]}…  {name}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
