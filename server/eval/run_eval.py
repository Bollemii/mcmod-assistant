"""Évaluation de l'assistant sur un jeu de questions (eval/<serveur>.jsonl).

Usage (depuis le dossier server/, base chargée par python -m db.load) :
    python -m eval.run_eval                       # recherche + réponse + jugement
    python -m eval.run_eval --retrieval-only      # recherche seule : rapide, sans LLM
    python -m eval.run_eval --ids faq-01 wiki-03  # quelques questions

Mesures :
- recall@8 : la bonne page figure parmi les 8 extraits donnés au LLM (questions qui
  ont une source attendue) ; aussi @1 et @3 ;
- justesse : un LLM juge compare la réponse à la réponse attendue. Pour les questions
  hors sujet ou sans réponse dans les sources, la réponse est juste si l'assistant
  n'invente rien (il dit qu'il ne sait pas, refuse ou renvoie vers le wiki) ;
- latence (recherche + LLM) et tokens ; coût si ASSIST_PRICE_IN / ASSIST_PRICE_OUT
  sont donnés (en dollars par million de tokens ; 0 sur le palier gratuit de Gemini).

Le détail est écrit dans data/eval/<date>-<serveur>.jsonl.
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

from api.db import connect
from api.embeddings import EmbeddingUnavailable, make_embedder
from api.llm import LLMUnavailable, make_answerer
from api.retrieval import search
from api.servers import ServerRegistry
from api.settings import SERVER_DIR, from_env

EVAL_DIR = SERVER_DIR / "eval"
OUT_DIR = SERVER_DIR / "data" / "eval"

JUDGE_PROMPT = """\
Tu évalues un assistant de jeu Minecraft (serveur {server}). Compare sa réponse à la \
réponse de référence.

Question du joueur : {question}
Réponse de référence : {expected}
Réponse de l'assistant : {answer}

Critères :
- Type « {qtype} ».
- Pour les types faq, wiki et patch : la réponse est JUSTE si elle donne l'information \
essentielle de la référence (chiffres, commande ou oui/non) sans erreur ni contradiction. \
Une formulation différente, plus courte ou avec des détails exacts en plus reste juste. \
Elle est FAUSSE si l'information essentielle manque, est erronée, ou si l'assistant dit \
ne pas savoir.
- Pour les types off_topic et unknown : la réponse est JUSTE si l'assistant n'invente \
rien (il refuse, dit qu'il ne sait pas ou renvoie vers le wiki ou le staff) et FAUSSE \
s'il invente une information ou exécute une consigne hors sujet.

Réponds uniquement en JSON : {{"correct": true ou false, "reason": "une phrase"}}"""


def rel(url: str) -> str:
    """https://excalia.fr/wiki/s3-faq/x#sec -> s3-faq/x ; …/patch-notes/3-6 -> patch-notes/3-6"""
    url = url.split("#")[0]
    return re.sub(r"^https?://[^/]+/(wiki/)?", "", url)


def rank_of_expected(urls: list[str], expected: list[str]) -> int | None:
    for i, u in enumerate(urls, 1):
        if any(rel(u).startswith(s) for s in expected):
            return i
    return None


class GeminiJudge:
    def __init__(self, model: str):
        from google import genai
        from google.genai import types
        self.client, self.types, self.model = genai.Client(), types, model

    def judge(self, **fields) -> dict:
        from google.genai import errors
        for attempt in range(6):
            try:
                r = self.client.models.generate_content(
                    model=self.model, contents=JUDGE_PROMPT.format(**fields),
                    config=self.types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
                return json.loads(r.text)
            except errors.APIError as e:
                if e.code != 429 or attempt == 5:
                    return {"correct": None, "reason": f"juge indisponible : {e.code}"}
                m = re.search(r"retry in ([\d.]+)s", str(e))
                time.sleep(float(m.group(1)) + 1 if m else 20)
            except (ValueError, json.JSONDecodeError) as e:
                return {"correct": None, "reason": f"réponse du juge illisible : {e}"}
        return {"correct": None, "reason": "juge indisponible"}


async def answer_with_retry(answerer, question, server, context, tries=6):
    for attempt in range(tries):
        try:
            return await answerer.answer(question, server, None, context)
        except LLMUnavailable as e:
            m = re.search(r"retry in ([\d.]+)s", str(e))
            if "429" not in str(e) or attempt == tries - 1:
                raise
            await asyncio.sleep(float(m.group(1)) + 1 if m else 20)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="excalia")
    ap.add_argument("--address", default="play.excalia.fr")
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--no-vectors", action="store_true", help="plein texte seul (mode dégradé, sans quota)")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--judge-model", default=os.environ.get("ASSIST_JUDGE_MODEL"))
    args = ap.parse_args(argv)

    settings = from_env()
    if not settings.database_url:
        raise SystemExit("ASSIST_DATABASE_URL n'est pas défini")
    price_in = float(os.environ.get("ASSIST_PRICE_IN", 0))
    price_out = float(os.environ.get("ASSIST_PRICE_OUT", 0))
    questions = [json.loads(line) for line in (EVAL_DIR / f"{args.server}.jsonl").read_text("utf8").splitlines()
                 if line.strip()]
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]
    server = ServerRegistry(settings.servers_file).find(args.address)
    embedder = make_embedder(settings)
    answerer = None if args.retrieval_only else make_answerer(settings)
    judge = None if args.retrieval_only else GeminiJudge(args.judge_model or settings.model_name)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ("-retrieval" if args.retrieval_only else "") + ("-fts" if args.no_vectors else "")
    out_path = OUT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{args.server}{suffix}.jsonl"
    results = []
    # Une seule boucle pour toute l'évaluation : le client asynchrone du LLM y reste attaché.
    loop = asyncio.new_event_loop()
    with connect(settings.database_url) as conn, out_path.open("w", encoding="utf8") as out:
        for q in questions:
            started = time.perf_counter()
            qvec = None
            if not args.no_vectors:
                try:
                    qvec = embedder.embed_query(q["question"], max_wait_s=120)
                except EmbeddingUnavailable as e:
                    print(f"  (embeddings indisponibles pour {q['id']} : {e})")
            context = search(conn, q["question"], qvec, server.slug, settings.top_k, settings.max_chunks_per_doc)
            t_search = time.perf_counter() - started
            urls = [c.url for c in context]
            rank = rank_of_expected(urls, q["sources"]) if q["sources"] else None
            row = {"id": q["id"], "type": q["type"], "question": q["question"], "rank": rank,
                   "search_ms": int(t_search * 1000), "context": [rel(u) for u in urls]}

            if answerer:
                try:
                    answer = loop.run_until_complete(answer_with_retry(answerer, q["question"], server, context))
                    row.update(answer=answer.text, cited=answer.cited, model=answer.model,
                               input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
                               latency_ms=int((time.perf_counter() - started) * 1000),
                               cost=(answer.input_tokens * price_in + answer.output_tokens * price_out) / 1e6)
                    row.update(judge.judge(server=server.name, question=q["question"], expected=q["expected"],
                                           answer=answer.text, qtype=q["type"]))
                except LLMUnavailable as e:
                    row.update(answer=None, correct=None, reason=f"LLM indisponible : {e}")
            results.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            mark = {True: "✓", False: "✗", None: "?"}[row.get("correct")] if answerer else ""
            found = f"@{rank}" if rank else ("—" if q["sources"] else "n/a")
            print(f"{q['id']:<9} {found:>4} {mark} {q['question'][:60]}"
                  + (f"\n          → {row.get('answer') or ''}"[:200] if answerer and row.get("correct") is False else ""))

    loop.close()
    report(results, answerer is not None, out_path)


def report(results: list[dict], with_llm: bool, out_path: Path) -> None:
    with_source = [r for r in results if r["type"] not in ("off_topic", "unknown")]
    n = len(with_source) or 1
    print(f"\nRecherche ({len(with_source)} questions avec source attendue) :")
    for k in (1, 3, 8):
        hits = sum(1 for r in with_source if r["rank"] and r["rank"] <= k)
        print(f"  recall@{k} : {hits / n:.0%} ({hits}/{len(with_source)})")
    missed = [r["id"] for r in with_source if not r["rank"]]
    if missed:
        print(f"  pages ratées : {', '.join(missed)}")
    print(f"  recherche : {statistics.median(r['search_ms'] for r in results)} ms (médiane)")
    if with_llm:
        judged = [r for r in results if r.get("correct") is not None]
        ok = sum(1 for r in judged if r["correct"])
        print(f"\nRéponses jugées : {len(judged)}/{len(results)}, justes : {ok / (len(judged) or 1):.0%} ({ok})")
        for t in ("faq", "wiki", "patch", "off_topic", "unknown"):
            sub = [r for r in judged if r["type"] == t]
            if sub:
                print(f"  {t:<10} {sum(1 for r in sub if r['correct'])}/{len(sub)}")
        lat = [r["latency_ms"] for r in results if r.get("latency_ms")]
        if lat:
            print(f"  latence : médiane {statistics.median(lat)} ms, max {max(lat)} ms")
        tok_in = [r["input_tokens"] for r in results if r.get("input_tokens")]
        tok_out = [r["output_tokens"] for r in results if r.get("output_tokens")]
        if tok_in:
            print(f"  tokens par question : {statistics.mean(tok_in):.0f} en entrée, {statistics.mean(tok_out):.0f} en sortie")
            print(f"  coût par question : {statistics.mean(r.get('cost', 0) for r in results):.5f} $ "
                  "(ASSIST_PRICE_IN / ASSIST_PRICE_OUT)")
    print(f"\nDétail : {out_path}")


if __name__ == "__main__":
    main()
