"""
extract_extra_points.py

Fase 2c da extração: obtém as estatísticas de EXTRA POINTS (PAT) da
temporada, acumuladas por kicker, via a página de Logs de cada um:

    https://www.nfl.com/players/<slug-do-kicker>/stats/logs/<ano>/

Por que via scraper individual (e não uma categoria de líderes): a
listagem "Field Goals" e "Kickoffs" do nfl.com não trazem nenhuma coluna
de Extra Points — essa informação só existe na página de cada jogador.
Como só existem ~35-40 kickers na liga (um por time), isso é uma
raspagem pequena e rápida, sem necessidade de paginação.

O método: soma, semana a semana, as colunas "XP Att", "XPM" e o "Blk"
de extra point (a coluna de bloqueio que vem LOGO DEPOIS de "XPM"/"Pct"
na tabela — há uma outra coluna "BLK" no início da tabela, essa é de
field goal bloqueado, não de extra point; por isso a leitura é feita
por posição relativa ao cabeçalho "XP Att", não por nome isolado).

Entrada: o CSV gerado por extract_players_roster.py (para saber quais
jogadores são kickers de cada time — position == 'K').

Saída: um CSV `extra_points_<ano>.csv` com colunas:
    player_id_team, season, extra_point_attempts, extra_points_made,
    extra_point_pct, extra_points_blocked

Uso:
    python extract_extra_points.py --roster players_roster.csv --year 2026 \
        --output extra_points_2026.csv
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


def load_kickers(roster_csv: Path) -> list[dict]:
    kickers = []
    with open(roster_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("position") or "").strip().upper() == "K":
                kickers.append({
                    "player_id": row["player_id"],
                    "team_id": row["team_id"],
                })
    return kickers


def to_int(text: str) -> int:
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0


def fetch_extra_points(player_id: str, year: int, session: requests.Session) -> dict:
    url = f"https://www.nfl.com/players/{player_id}/stats/logs/{year}/"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    totals = {"extra_point_attempts": 0, "extra_points_made": 0, "extra_points_blocked": 0}

    # Mesmo cuidado do extract_qb_games.py: buscar o cabeçalho exato
    # "Regular Season" (maiúsculas) em tags de heading, não em texto solto
    # (o seletor de temporada no topo da página usa "Regular season",
    # minúsculo, e apareceria antes na busca case-insensitive).
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

    try:
        idx_xp_att = header_cells.index("XP Att")
    except ValueError:
        print(f"  [AVISO] coluna 'XP Att' não encontrada para {player_id} "
              f"(kicker sem estatísticas de XP ainda?)", file=sys.stderr)
        return totals

    idx_xpm = idx_xp_att + 1     # "XPM" vem logo depois de "XP Att"
    idx_xp_blk = idx_xp_att + 3  # "XP Att", "XPM", "Pct", "Blk" (bloqueio de XP)

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) <= idx_xp_blk:
            continue
        totals["extra_point_attempts"] += to_int(cells[idx_xp_att].get_text(strip=True))
        totals["extra_points_made"] += to_int(cells[idx_xpm].get_text(strip=True))
        totals["extra_points_blocked"] += to_int(cells[idx_xp_blk].get_text(strip=True))

    return totals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--roster", required=True, help="CSV gerado por extract_players_roster.py")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()

    output = args.output or f"extra_points_{args.year}.csv"

    kickers = load_kickers(Path(args.roster))
    print(f"{len(kickers)} kickers encontrados no roster.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    rows = []
    for kicker in kickers:
        player_id = kicker["player_id"]
        team_id = kicker["team_id"]
        print(f"Extraindo XP: {player_id} ({team_id})...")
        try:
            totals = fetch_extra_points(player_id, args.year, session)
        except requests.RequestException as e:
            print(f"  [ERRO] falha ao buscar {player_id}: {e}", file=sys.stderr)
            continue

        att = totals["extra_point_attempts"]
        made = totals["extra_points_made"]
        pct = round(100 * made / att, 1) if att > 0 else 0

        rows.append({
            "player_id_team": f"{player_id}-{team_id}",
            "season": args.year,
            "extra_point_attempts": att,
            "extra_points_made": made,
            "extra_point_pct": pct,
            "extra_points_blocked": totals["extra_points_blocked"],
        })
        print(f"  -> Att={att} Made={made} Pct={pct} Blk={totals['extra_points_blocked']}")
        time.sleep(args.delay)

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "player_id_team", "season", "extra_point_attempts",
            "extra_points_made", "extra_point_pct", "extra_points_blocked",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nConcluído: {len(rows)} kickers salvos em {output}")


if __name__ == "__main__":
    main()