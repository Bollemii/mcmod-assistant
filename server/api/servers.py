"""Retrouve un serveur de servers.toml à partir de l'adresse envoyée par le client."""
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerInfo:
    slug: str
    name: str
    description: str
    mc_version: str
    wiki_url: str | None


def normalize(address: str) -> str:
    """'Play.Excalia.fr:25565' -> 'play.excalia.fr'."""
    address = address.strip().lower().rstrip(".")
    if address.count(":") == 1:          # pas une IPv6
        address = address.split(":")[0]
    return address


class ServerRegistry:
    def __init__(self, path: Path):
        self.by_address: dict[str, ServerInfo] = {}
        for slug, conf in tomllib.loads(path.read_text("utf8")).items():
            info = ServerInfo(slug=slug, name=conf["name"], description=conf.get("description", ""),
                              mc_version=conf.get("mc_version", ""), wiki_url=conf.get("wiki_url"))
            for address in conf.get("addresses", []):
                self.by_address[normalize(address)] = info

    def find(self, address: str | None) -> ServerInfo | None:
        if not address:
            return None
        address = normalize(address)
        # Un sous-domaine d'une adresse connue désigne le même serveur (eu.play.excalia.fr).
        while address:
            if address in self.by_address:
                return self.by_address[address]
            address = address.partition(".")[2]
        return None
