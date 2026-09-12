"""
extract_qb_games.py

Fase 2b da extração: obtém os dados de JOGO por jogo de cada time
(semana, adversário, mandante/visitante, resultado, placar) para
alimentar a tabela `games`, usando a página de Logs de TODOS os QBs
de cada time:

    https://www.nfl.com/players/<slug-do-qb>/stats/logs/<ano>/

Por que via QB: essa página traz o jogo a jogo da temporada inteira
(WK, Game Date, OPP, RESULT) mesmo em semanas em que o jogador não
teve estatística (célula vazia, mas a linha do jogo aparece) — ao
contrário da seção "Recent Games" da página resumo, que só mostra o
jogo mais recente.

Por que TODOS os QBs do time (não só o titular): garante cobertura
mesmo que o titular saia machucado no meio da temporada e o back-up
assuma — cada QB só aparece nas semanas em que esteve no elenco
ativo daquele jogo; juntando os QBs do time, cobrimos a temporada
inteira. Linhas duplicadas (mesma semana reportada por 2 QBs do
mesmo time) são eliminadas na deduplicação final.

Entrada: o CSV gerado por extract_players_roster.py (para saber
quais jogadores são QB de cada time).

Saída: um CSV `games_<ano>.csv` com colunas:
    team_id, season, week, opponent, home_away, win_loss,
    made_points, suffered_points

Uso:
    python extract_qb_games.py --roster players_roster.csv --year 2026 \
        --output games_2026.csv
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

RESULT_RE = re.compile(r"^([WLT])\s+(\d+)\s*-\s*(\d+)$")


def load_qbs_by_team(roster_csv: Path) -> dict[str, list[str]]:
    qbs_by_team: dict[str, list[str]] = {}
    with open(roster_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("position") or "").strip().upper() == "QB":
                qbs_by_team.setdefault(row["team_id"], []).append(row["player_id"])
    return qbs_by_team


def parse_opponent(opp_cell: str):
    """'@Rams' -> ('Rams', 'Away'); 'Rams' -> ('Rams', 'Home')."""
    opp_cell = opp_cell.strip()
    if opp_cell.startswith("@"):
        return opp_cell[1:].strip(), "Away"
    return opp_cell, "Home"


def parse_result(result_cell: str):
    """'W 27 - 7' -> ('W', 27, 7)."""
    m = RESULT_RE.match(result_cell.strip())
    if not m:
        return None, None, None
    return m.group(1), int(m.group(2)), int(m.group(3))


def fetch_qb_games(player_id: str, team_id: str, year: int,
                    session: requests.Session) -> list[dict]:
    url = f"https://www.nfl.com/players/{player_id}/stats/logs/{year}/"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    games = []

    # A página tem 3 seções possíveis: Preseason, Regular Season, Post Season.
    # Só nos interessa a temporada regular.
    # IMPORTANTE: buscar apenas em tags de cabeçalho (h2/h3/h4), com o texto
    # exato "Regular Season" (maiúsculas). O seletor de temporada no topo da
    # página também contém o texto "Regular season" (minúsculo, dentro de um
    # botão/lista, não um cabeçalho) — uma busca case-insensitive por texto
    # solto encontra esse seletor primeiro e pega a tabela errada (a de
    # pré-temporada, que vem logo depois dele).
    heading = None
    for tag in soup.find_all(re.compile(r"^h[1-4]$")):
        if tag.get_text(strip=True) == "Regular Season":
            heading = tag
            break
    if heading is None:
        print(f"  [AVISO] cabeçalho 'Regular Season' não encontrado para {player_id}", file=sys.stderr)
        return games

    table = heading.find_next("table")
    if table is None:
        return games

    header_cells = [th.get_text(strip=True).lower() for th in table.find("thead").find_all("th")] \
        if table.find("thead") else []

    try:
        idx_wk = header_cells.index("wk")
        idx_opp = header_cells.index("opp")
        idx_result = header_cells.index("result")
    except ValueError:
        print(f"  [AVISO] cabeçalho inesperado para {player_id}, pulando", file=sys.stderr)
        return games

    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) <= max(idx_wk, idx_opp, idx_result):
            continue
        week_text = cells[idx_wk].get_text(strip=True)
        if not week_text.isdigit():
            continue
        week = int(week_text)
        opponent, home_away = parse_opponent(cells[idx_opp].get_text(strip=True))
        win_loss, made_points, suffered_points = parse_result(cells[idx_result].get_text(strip=True))
        if win_loss is None:
            continue  # jogo ainda não ocorreu / sem resultado

        games.append({
            "team_id": team_id,
            "season": year,
            "week": week,
            "opponent": opponent,
            "home_away": home_away,
            "win_loss": win_loss,
            "made_points": made_points,
            "suffered_points": suffered_points,
        })

    return games


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--roster", required=True, help="CSV gerado por extract_players_roster.py")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()

    output = args.output or f"games_{args.year}.csv"

    qbs_by_team = load_qbs_by_team(Path(args.roster))
    print(f"{sum(len(v) for v in qbs_by_team.values())} QBs encontrados em "
          f"{len(qbs_by_team)} times.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    seen = set()  # (team_id, week) já coletados, pra deduplicar entre QBs do mesmo time
    all_games = []

    for team_id, qb_ids in qbs_by_team.items():
        print(f"Time {team_id}: {len(qb_ids)} QB(s) -> {qb_ids}")
        for player_id in qb_ids:
            try:
                games = fetch_qb_games(player_id, team_id, args.year, session)
            except requests.RequestException as e:
                print(f"  [ERRO] falha ao buscar {player_id}: {e}", file=sys.stderr)
                continue

            new_count = 0
            for g in games:
                key = (g["team_id"], g["week"])
                if key in seen:
                    continue
                seen.add(key)
                all_games.append(g)
                new_count += 1
            print(f"  {player_id}: {len(games)} jogos lidos, {new_count} novos")
            time.sleep(args.delay)

    all_games.sort(key=lambda g: (g["team_id"], g["week"]))

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "team_id", "season", "week", "opponent", "home_away",
            "win_loss", "made_points", "suffered_points",
        ])
        writer.writeheader()
        writer.writerows(all_games)

    print(f"\nConcluído: {len(all_games)} jogos (time-semana) salvos em {output}")


if __name__ == "__main__":
    main()