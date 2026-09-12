"""
extract_category_stats.py

Fase 2a da extração: obtém as estatísticas ACUMULADAS DA TEMPORADA de todos
os jogadores, via as páginas de "líderes por categoria" do nfl.com
(https://www.nfl.com/stats/player-stats/category/<categoria>/<ano>/reg/all/<ordenacao>/desc),
em vez de visitar a página de cada um dos milhares de jogadores individualmente.

Cada categoria já retorna TODOS os jogadores com estatística naquela
categoria na temporada (não só os líderes) — a página pagina via cursor
opaco (parâmetro `aftercursor`), seguido através do link "Next Page".

Categoria "Tackles" fica de fora propositalmente: a página está retornando
"No Stats Available" no nfl.com (bug atual do site, confirmado em 2025 e
2026 com diferentes critérios de ordenação). As estatísticas de tackles/
sacks/safeties/pass-defended precisam vir do scraper por jogador
(extract_defense_stats.py, próxima etapa).

Saída: um CSV por categoria em --output-dir, com colunas =
[player_id, player_name] + as colunas exibidas na tabela (nomes
normalizados: minúsculo, espaços/símbolos -> underscore).

Uso:
    python extract_category_stats.py --year 2026 --output-dir ./stats_2026
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_URL = "https://www.nfl.com/stats/player-stats/category"

# categoria (slug da URL) -> critério de ordenação padrão (necessário na URL,
# mas não afeta quais jogadores aparecem, só a ordem)
CATEGORIES = {
    "passing": "passingyards",
    "rushing": "rushingyards",
    "receiving": "receivingreceptions",
    "fumbles": "defensiveforcedfumble",
    "interceptions": "defensiveinterceptions",
    "field-goals": "kickingfgmade",
    "kickoffs": "kickofftotal",
    "kickoff-returns": "kickreturnsaverageyards",
    "punts": "puntingaverageyards",
    "punt-returns": "puntreturnsaverageyards",
    # "tackles" fica de fora: página quebrada no nfl.com no momento
}

PLAYER_URL_RE = re.compile(r"/players/([a-z0-9\-]+)/?$")


def normalize_header(text: str) -> str:
    text = text.strip().lower()
    text = text.replace("%", "pct").replace("+", "plus").replace("/", "_per_")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def parse_table(html: str):
    """Extrai (linhas, próxima_url) de uma página de categoria."""
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for candidate in soup.find_all("table"):
        if candidate.find("a", href=PLAYER_URL_RE):
            table = candidate
            break
    if table is None:
        return [], None

    header_cells = table.find("thead").find_all("th") if table.find("thead") else table.find_all("th")
    headers = [normalize_header(th.get_text(" ", strip=True)) for th in header_cells]
    # a 1a coluna é sempre "Player" -> vira player_name / player_id
    stat_headers = headers[1:]

    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        link = cells[0].find("a", href=PLAYER_URL_RE)
        if link is None:
            continue
        m = PLAYER_URL_RE.search(link["href"])
        player_id = m.group(1)
        player_name = link.get_text(strip=True)

        row = {"player_id": player_id, "player_name": player_name}
        for i, header in enumerate(stat_headers, start=1):
            row[header] = cells[i].get_text(strip=True) if i < len(cells) else ""
        rows.append(row)

    # link "Next Page"
    next_url = None
    next_link = soup.find("a", string=re.compile(r"Next Page", re.I))
    if next_link and next_link.get("href"):
        next_url = next_link["href"]

    return rows, next_url


def fetch_category(category: str, sort: str, year: int, session: requests.Session,
                    delay: float, max_pages: int) -> list[dict]:
    url = f"{BASE_URL}/{category}/{year}/reg/all/{sort}/desc"
    all_rows = []
    page_num = 1

    while url and page_num <= max_pages:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        rows, next_url = parse_table(resp.text)
        print(f"  página {page_num}: {len(rows)} linhas")
        all_rows.extend(rows)

        # o link "Next Page" pode vir como caminho relativo (ex: "/stats/...")
        url = urljoin(resp.url, next_url) if next_url else None
        page_num += 1
        if url:
            time.sleep(delay)

    if page_num > max_pages:
        print(f"  [AVISO] atingiu max_pages={max_pages} — pode haver mais dados não coletados",
              file=sys.stderr)

    return all_rows


def write_csv(rows: list[dict], path: Path):
    if not rows:
        print(f"  [AVISO] nenhuma linha para {path.name}, arquivo não gerado", file=sys.stderr)
        return
    # união de todas as colunas (algumas linhas podem ter colunas ausentes)
    fieldnames = list(rows[0].keys())
    for row in rows[1:]:
        for k in row.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output-dir", default="./stats_output")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="segundos de espera entre páginas/categorias")
    parser.add_argument("--max-pages", type=int, default=100,
                         help="trava de segurança contra loop infinito de paginação")
    parser.add_argument("--categories", nargs="*", default=list(CATEGORIES.keys()),
                         help="subconjunto de categorias a rodar (padrão: todas)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for category in args.categories:
        if category not in CATEGORIES:
            print(f"[AVISO] categoria desconhecida: {category}", file=sys.stderr)
            continue
        sort = CATEGORIES[category]
        print(f"Extraindo categoria: {category} (ano={args.year})...")
        rows = fetch_category(category, sort, args.year, session, args.delay, args.max_pages)
        out_path = out_dir / f"{category.replace('-', '_')}_{args.year}.csv"
        write_csv(rows, out_path)
        print(f"  -> {len(rows)} jogadores salvos em {out_path}")
        time.sleep(args.delay)

    print("\nConcluído.")


if __name__ == "__main__":
    main()