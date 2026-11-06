"""Jetons d'accès des joueurs.

Le fichier (server/config/tokens.txt par défaut) ne contient que l'empreinte SHA-256
de chaque jeton, suivie de son type et d'un nom libre :

    3f2a…e9  joueur  Emilien
    8c41…07  admin   Emilien (admin)

Deux types de jetons :
- joueur : /assist <question>, /assist maj (avec un délai minimal entre deux mises à jour) ;
- admin  : en plus, /assist maj force (sans délai, reconstruction complète) et /assist sources.
Une ligne sans type (ancien format « empreinte  nom ») est un jeton joueur.

Créer un jeton (affiché une seule fois) :
    python -m api.tokens new "Emilien"
    python -m api.tokens new --admin "Emilien"
Lister les jetons :
    python -m api.tokens list
"""
import hashlib
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from .settings import from_env

PLAYER, ADMIN = "joueur", "admin"
ROLES = (PLAYER, ADMIN)


@dataclass(frozen=True)
class TokenInfo:
    name: str
    role: str = PLAYER

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load(path: Path) -> dict[str, TokenInfo]:
    """empreinte -> TokenInfo."""
    tokens = {}
    if path.exists():
        for line in path.read_text("utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                h, _, rest = line.partition(" ")
                role, _, name = rest.strip().partition(" ")
                if role not in ROLES:      # ancien format, sans type
                    role, name = PLAYER, rest
                tokens[h] = TokenInfo(name.strip() or "?", role)
    return tokens


def create(path: Path, name: str, role: str = PLAYER) -> str:
    if role not in ROLES:
        raise ValueError(f"type de jeton inconnu : {role}")
    token = "mca_" + secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf8") as f:
        f.write(f"{digest(token)}  {role:<6}  {name}\n")
    return token


def main(argv: list[str]) -> None:
    path = from_env().tokens_file
    if argv[:1] == ["new"]:
        args = argv[1:]
        role = ADMIN if "--admin" in args else PLAYER
        name = " ".join(a for a in args if a != "--admin")
        if not name:
            print(__doc__)
            return
        token = create(path, name, role)
        print(f"Jeton {role} créé pour {name} (à copier maintenant, il ne sera plus affiché) :")
        print(token)
    elif argv[:1] == ["list"]:
        for h, info in load(path).items():
            print(f"{h[:12]}…  {info.role:<6}  {info.name}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
