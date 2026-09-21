"""
extract_players_roster.py

Fase 1 da extração: obtém a lista ATUALIZADA de jogadores da NFL a partir
das páginas de elenco (roster) de cada um dos 32 times.

Por que essa abordagem em vez de paginar o índice alfabético de jogadores:
  - Cada time tem UMA única página de roster (sem paginação / "Next Page").
  - A lista reflete o elenco atual: jogadores cortados/aposentados somem
    automaticamente, e novatos/contratados aparecem assim que são
    adicionados ao elenco oficial. Não precisamos manter uma lista
    estática de player_id ano a ano.
  - Total: 32 requisições por execução (uma por time), bem mais leve
    que varrer o índice alfabético completo de jogadores.

Cada coluna (No, Pos, Status, Height, Weight, Exp, College) é localizada
pelo texto do cabeçalho da tabela, não por uma posição fixa — o mesmo
método usado nos demais extratores do pipeline. Isso protege contra o
nfl.com reordenar, remover ou renomear colunas da tabela de roster: uma
coluna cujo cabeçalho não é reconhecido fica vazia (com aviso no log) em
vez de fazer outra coluna ler o valor errado.

Saída: um CSV com uma linha por jogador (player_id = slug da URL,
player_name, team_id, position, status, height, weight, experience,
college), pronto para popular/atualizar a tabela `players` no Supabase.

Uso:
    python extract_players_roster.py --output players_roster.csv
"""

import argparse
import csv
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Mapeamento team_id (sigla usada no banco) -> slug da URL no nfl.com
TEAM_SLUGS = {
    "BUF": "buffalo-bills", "MIA": "miami-dolphins", "NE": "new-england-patriots",
    "NYJ": "new-york-jets", "BAL": "baltimore-ravens", "CIN": "cincinnati-bengals",
    "CLE": "cleveland-browns", "PIT": "pittsburgh-steelers", "HOU": "houston-texans",
    "IND": "indianapolis-colts", "JAX": "jacksonville-jaguars", "TEN": "tennessee-titans",
    "DEN": "denver-broncos", "KC": "kansas-city-chiefs", "LV": "las-vegas-raiders",
    "LAC": "los-angeles-chargers", "DAL": "dallas-cowboys", "NYG": "new-york-giants",
    "PHI": "philadelphia-eagles", "WAS": "washington-commanders", "CHI": "chicago-bears",
    "DET": "detroit-lions", "GB": "green-bay-packers", "MIN": "minnesota-vikings",
    "ATL": "atlanta-falcons", "CAR": "carolina-panthers", "NO": "new-orleans-saints",
    "TB": "tampa-bay-buccaneers", "ARI": "arizona-cardinals", "LAR": "los-angeles-rams",
    "SF": "san-francisco-49ers", "SEA": "seattle-seahawks",
}

PLAYER_URL_RE = re.compile(r"/players/([a-z0-9\-]+)/?$")

# campo do PlayerRow -> possíveis textos de cabeçalho (minúsculo) que o
# identificam na tabela de roster; o primeiro que bater é usado.
ROSTER_COLUMN_CANDIDATES = {
    "jersey_number": ["no", "no.", "#"],
    "position": ["pos", "position"],
    "status": ["status"],
    "height_in": ["height", "ht"],
    "weight_lb": ["weight", "wt"],
    "experience": ["exp", "experience"],
    "college": ["college"],
}


@dataclass
class PlayerRow:
    player_id: str          # slug, ex: 'brock-purdy'
    player_name: str
    team_id: str
    jersey_number: Optional[str]
    position: Optional[str]
    status: Optional[str]
    height_in: Optional[str]
    weight_lb: Optional[str]
    experience: Optional[str]
    college: Optional[str]


def locate_roster_columns(header_cells: list[str]) -> dict[str, int]:
    """Recebe o texto (já em minúsculo) de cada <th> do cabeçalho da tabela
    de roster e retorna, por campo do PlayerRow, o índice da coluna
    correspondente. Um campo cujo cabeçalho não é reconhecido simplesmente
    não aparece no dict retornado."""
    col_idx: dict[str, int] = {}
    for field, candidates in ROSTER_COLUMN_CANDIDATES.items():
        for candidate in candidates:
            if candidate in header_cells:
                col_idx[field] = header_cells.index(candidate)
                break
    return col_idx


def fetch_team_roster(team_id: str, slug: str, session: requests.Session) -> list[PlayerRow]:
    url = f"https://www.nfl.com/teams/{slug}/roster"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows: list[PlayerRow] = []

    table = None
    for candidate in soup.find_all("table"):
        header_text = candidate.get_text(" ", strip=True).lower()
        if "player" in header_text and "pos" in header_text:
            table = candidate
            break

    if table is None or table.find("tbody") is None:
        print(f"[AVISO] Nenhuma tabela de roster reconhecida para {team_id} ({url})", file=sys.stderr)
        return rows

    header_cells = [th.get_text(strip=True).lower() for th in table.find("thead").find_all("th")] \
        if table.find("thead") else []
    col_idx = locate_roster_columns(header_cells)

    missing_fields = [f for f in ROSTER_COLUMN_CANDIDATES if f not in col_idx]
    if missing_fields:
        print(f"[AVISO] colunas não reconhecidas no cabeçalho do roster de {team_id} "
              f"({url}): {missing_fields} — ficarão vazias nesta execução", file=sys.stderr)

    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        link = cells[0].find("a", href=PLAYER_URL_RE)
        if link is None:
            continue

        m = PLAYER_URL_RE.search(link["href"])
        player_id = m.group(1)
        player_name = link.get_text(strip=True)

        def cell_text(field: str) -> Optional[str]:
            i = col_idx.get(field)
            return cells[i].get_text(strip=True) if i is not None and i < len(cells) else None

        rows.append(PlayerRow(
            player_id=player_id,
            player_name=player_name,
            team_id=team_id,
            jersey_number=cell_text("jersey_number"),
            position=cell_text("position"),
            status=cell_text("status"),
            height_in=cell_text("height_in"),
            weight_lb=cell_text("weight_lb"),
            experience=cell_text("experience"),
            college=cell_text("college"),
        ))

    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="players_roster.csv")
    parser.add_argument("--delay", type=float, default=1.5,
                         help="segundos de espera entre requisições (respeito ao site)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    all_rows: list[PlayerRow] = []
    for team_id, slug in TEAM_SLUGS.items():
        print(f"Extraindo elenco: {team_id} ({slug})...")
        try:
            team_rows = fetch_team_roster(team_id, slug, session)
            print(f"  -> {len(team_rows)} jogadores encontrados")
            all_rows.extend(team_rows)
        except requests.RequestException as e:
            print(f"[ERRO] Falha ao buscar {team_id}: {e}", file=sys.stderr)
        time.sleep(args.delay)

    if not all_rows:
        print("Nenhum jogador extraído. Encerrando sem gerar CSV.", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(all_rows[0]).keys()))
        writer.writeheader()
        for row in all_rows:
            writer.writerow(asdict(row))

    print(f"\nConcluído: {len(all_rows)} jogadores salvos em {args.output}")


if __name__ == "__main__":
    main()