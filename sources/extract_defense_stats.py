"""
extract_defense_stats.py

Fase 2d da extração: obtém as estatísticas de TACKLES/SACKS/SAFETIES/
PASS DEFENDED da temporada, acumuladas por jogador defensivo, via a
página de Logs de cada um:

    https://www.nfl.com/players/<slug>/stats/logs/<ano>/

Por que via scraper individual (e não a categoria "Tackles" de líderes):
a categoria está retornando "No Stats Available" no nfl.com no momento
(bug confirmado em 2025 e 2026, com diferentes critérios de ordenação).

O que este script NÃO coleta (de propósito, para não duplicar/conflitar
com outras fontes já usadas):
  - Interceptions (INT/YDS/AVG/LNG/TDS) -> vem da categoria "Interceptions"
    (extract_category_stats.py), fonte mais confiável e sem paginação
    manual por jogador.
  - Forced Fumbles / Fumble Recoveries (FF/FR) -> vem da categoria
    "Fumbles" (também via extract_category_stats.py).

Alvo: jogadores cujo `position` no roster seja uma posição defensiva
(DB, FS, CB, S, LB, DE, OLB, MLB, ILB, DT, NT — mesma lista de
`category = 'Defense'` usada no seed de Positions).

Observação sobre jogadores lesionados/reserva: quando um jogador sai de
Injured Reserve, as semanas seguintes simplesmente não aparecem na
tabela de Logs (diferente de uma semana de bye/inativo, que aparece com
WK/OPP/RESULT preenchidos e stats em branco). O script soma só as
semanas que efetivamente aparecem, então o resultado já reflete
corretamente a temporada disputada até aqui.

Entrada: o CSV gerado por extract_players_roster.py.

Saída: um CSV `defense_<ano>.csv` com colunas:
    player_id_team, season, combined_tackles, solo_tackles,
    assisted_tackles, sacks, safeties, pass_defended

Uso:
    python extract_defense_stats.py --roster players_roster.csv --year 2026 \
        --output defense_2026.csv
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFENSIVE_POSITIONS = {
    "DB", "FS", "CB", "S", "SAF", "LB", "DE", "OLB", "MLB", "ILB", "DT", "NT",
}


def load_defenders(roster_csv: Path) -> list[dict]:
    defenders = []
    with open(roster_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pos = (row.get("position") or "").strip().upper()
            if pos in DEFENSIVE_POSITIONS:
                defenders.append({"player_id": row["player_id"], "team_id": row["team_id"]})
    return defenders


def to_number(text: str) -> float:
    text = (text or "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_defense_totals(player_id: str, year: int, session: requests.Session) -> dict:
    url = f"https://www.nfl.com/players/{player_id}/stats/logs/{year}/"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    totals = {
        "combined_tackles": 0.0, "solo_tackles": 0.0, "assisted_tackles": 0.0,
        "sacks": 0.0, "safeties": 0.0, "pass_defended": 0.0,
    }

    # mesmo cuidado das outras etapas: cabeçalho exato "Regular Season"
    # em tag de heading, não o seletor de temporada (que usa minúsculo).
    heading = None
    for tag in soup.find_all(re.compile(r"^h[1-4]$")):
        if tag.get_text(strip=True) == "Regular Season":
            heading = tag
            break
    if heading is None:
        print(f"  [AVISO] cabeçalho 'Regular Season' não encontrado para {player_id}", file=sys.stderr)
        return totals

    table = heading.find_next("table")
    if table is None:
        return totals

    header_cells = [th.get_text(strip=True) for th in table.find("thead").find_all("th")] \
        if table.find("thead") else []

    col_map = {
        "combined_tackles": "Total",
        "solo_tackles": "Solo",
        "assisted_tackles": "AST",
        "sacks": "SCK",
        "safeties": "SFTY",
        "pass_defended": "PDEF",
    }
    try:
        idx = {key: header_cells.index(label) for key, label in col_map.items()}
    except ValueError:
        print(f"  [AVISO] cabeçalho de defesa não reconhecido para {player_id}: {header_cells}",
              file=sys.stderr)
        return totals

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) <= max(idx.values()):
            continue
        for key, i in idx.items():
            totals[key] += to_number(cells[i].get_text(strip=True))

    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--roster", required=True, help="CSV gerado por extract_players_roster.py")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()

    output = args.output or f"defense_{args.year}.csv"

    defenders = load_defenders(Path(args.roster))
    print(f"{len(defenders)} jogadores defensivos encontrados no roster.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    rows = []
    for i, defender in enumerate(defenders, start=1):
        player_id = defender["player_id"]
        team_id = defender["team_id"]
        print(f"[{i}/{len(defenders)}] Extraindo defense: {player_id} ({team_id})...")
        try:
            totals = fetch_defense_totals(player_id, args.year, session)
        except requests.RequestException as e:
            print(f"  [ERRO] falha ao buscar {player_id}: {e}", file=sys.stderr)
            continue

        # pula jogadores sem nenhuma estatística ainda (ex: reserva que não jogou)
        if all(v == 0 for v in totals.values()):
            time.sleep(args.delay)
            continue

        rows.append({
            "player_id_team": f"{player_id}-{team_id}",
            "season": args.year,
            "combined_tackles": int(totals["combined_tackles"]),
            "solo_tackles": int(totals["solo_tackles"]),
            "assisted_tackles": int(totals["assisted_tackles"]),
            "sacks": totals["sacks"],
            "safeties": int(totals["safeties"]),
            "pass_defended": int(totals["pass_defended"]),
        })
        time.sleep(args.delay)

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "player_id_team", "season", "combined_tackles", "solo_tackles",
            "assisted_tackles", "sacks", "safeties", "pass_defended",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nConcluído: {len(rows)} jogadores defensivos com estatística salvos em {output}")


if __name__ == "__main__":
    main()
