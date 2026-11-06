"""Découpage d'un fragment HTML (wiki, notes de patch…) en chunks prêts à être embeddés.

- Une section par titre (h1-h4) ; le chemin des titres est gardé dans chaque chunk.
- Les tableaux deviennent du markdown, en-tête répété dans chaque morceau.
- Les blocs trop longs sont coupés par ligne (listes) puis par phrase.
- Le texte alternatif des images est gardé : sur Excalia, il décrit souvent l'avant/après.
"""
import hashlib
import re

from bs4 import BeautifulSoup, NavigableString, Tag

MAX_CHARS = 1500       # taille cible d'un chunk
TABLE_ROWS = 15        # lignes de tableau max par chunk
HEADINGS = ("h1", "h2", "h3", "h4")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def table_to_md(table: Tag) -> list[str]:
    """Retourne une liste de blocs markdown (en-tête répété dans chaque bloc)."""
    header = [clean(th.get_text(" ")) for th in table.select("thead th")]
    rows = [[clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            for tr in table.select("tbody tr")]
    head = ""
    if header:
        head = "| " + " | ".join(header) + " |\n|" + "---|" * len(header) + "\n"
    blocks, body, count = [], "", 0
    for r in rows:
        line = "| " + " | ".join(r) + " |\n"
        if body and (count >= TABLE_ROWS or len(head) + len(body) + len(line) > MAX_CHARS):
            blocks.append((head + body).strip())
            body, count = "", 0
        body += line
        count += 1
    blocks.append((head + body).strip())
    return blocks


def split_long(text: str) -> list[str]:
    """Coupe un bloc trop long sur les lignes (listes) puis sur les phrases."""
    if len(text) <= MAX_CHARS:
        return [text]
    sep = "\n" if "\n" in text else " "
    parts = text.split("\n") if sep == "\n" else re.split(r"(?<=[.!?·—])\s+", text)
    out, buf = [], ""
    for p in parts:
        if buf and len(buf) + len(p) > MAX_CHARS:
            out.append(buf.strip())
            buf = ""
        buf += p + sep
    if buf.strip():
        out.append(buf.strip())
    return out


def text_with_alts(el: Tag) -> str:
    """Texte d'un élément, avec le texte alternatif des images entre crochets."""
    for img in el.find_all("img"):
        alt = clean(img.get("alt") or "")
        img.replace_with(f" [Image : {alt}] " if alt else " ")
    return clean(el.get_text(" "))


def block_to_md(el: Tag) -> list[str]:
    if el.name == "table":
        return table_to_md(el)
    if el.name in ("ul", "ol"):
        return split_long("\n".join("- " + text_with_alts(li) for li in el.find_all("li", recursive=False)))
    if el.name == "div" and el.find("table"):
        return [b for t in el.find_all("table") for b in table_to_md(t)]
    if el.name == "img":
        alt = clean(el.get("alt") or "")
        return [f"[Image : {alt}]"] if alt else []
    if el.name == "video":
        return []
    text = text_with_alts(el)
    return split_long(text) if text else []


def chunk_html(html: str, url: str, title: str, meta: dict, title_prefix: str | None = None) -> list[dict]:
    """Découpe le HTML d'une page en chunks, une section (titre h1-h4) à la fois.

    title_prefix remplace le titre en tête du chemin embeddé (ex. « Patch 3.6 (01/11/2026) »).
    """
    content = BeautifulSoup(html, "html.parser")

    # Sections : [(heading_path, anchor, [blocs markdown])]
    sections = [([], None, [])]
    for el in content.children:
        if isinstance(el, NavigableString) or not isinstance(el, Tag):
            continue
        if el.name in HEADINGS:
            sections.append(([clean(el.get_text(" "))], el.get("id"), []))
        else:
            sections[-1][2].extend(block_to_md(el))

    chunks = []
    for path, anchor, blocks in sections:
        # Regroupe les blocs jusqu'à MAX_CHARS ; un bloc de tableau reste entier.
        buf = ""
        for block in blocks + [None]:
            if block is None or (buf and len(buf) + len(block) > MAX_CHARS):
                if buf:
                    chunks.append({
                        "url": url + (f"#{anchor}" if anchor else ""),
                        **meta,
                        "title": title,
                        "heading_path": [title, *path],
                        "content": buf.strip(),
                    })
                buf = ""
            if block:
                buf += block + "\n\n"
    for i, c in enumerate(chunks):
        c["id"] = hashlib.sha1(f"{c['url']}|{i}".encode()).hexdigest()[:16]
        # Texte à embedder : le contexte de titres + le contenu
        head = [title_prefix or title, *c["heading_path"][1:]]
        c["embed_text"] = " > ".join(head) + "\n" + c["content"]
    return chunks
