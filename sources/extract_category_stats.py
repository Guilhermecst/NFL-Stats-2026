"""
extract_category_stats.py

Extrai a tabela `downs` (por TIME, temporada inteira, sem paginação — 32
linhas), via nfl.com/stats/team-stats/offense/downs/<ano>/reg/all. É uma
tabela por temporada, sem coluna `week`: o acumulado da página é o dado
certo por design, não uma limitação a contornar.

Saída: downs_<ano>.csv, uma linha por time, em --output-dir.

Uso:
    python extract_category_stats.py --year 2026 --output-dir ./stats_2026
"""

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TEAM_STATS_URL = "https://www.nfl.com/stats/team-stats/offense/downs"

# código da sigla usada no src do logo do time -> team_id do nosso banco
# (a maioria bate direto; só Arizona e os Rams usam um código diferente
# no nfl.com do que o padrão de sigla que adotamos)
LOGO_CODE_TO_TEAM_ID = {"AZ": "ARI", "LA": "LAR"}
LOGO_CODE_RE = re.compile(r"/clubs/logos/([A-Z]+)")


def fetch_team_downs(year: int, session: requests.Session) -> list[dict]:
    url = f"{TEAM_STATS_URL}/{year}/reg/all"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = None
    for candidate in soup.find_all("table"):
        if candidate.find("img", src=LOGO_CODE_RE):
            table = candidate
            break
    if table is None:
        print("  [AVISO] tabela de Downs não encontrada", file=sys.stderr)
        return []

    header_cells = [th.get_text(strip=True) for th in table.find("thead").find_all("th")] \
        if table.find("thead") else []
    col_map = {
        "3rd Att": "third_down_att", "3rd Md": "third_down_made",
        "4th Att": "fourth_down_att", "4th Md": "fourth_down_made",
        "Rec 1st": "receiving_first_downs", "Rec 1st%": "receiving_first_down_pct",
        "Rush 1st": "rushing_first_downs", "Rush 1st%": "rushing_first_down_pct",
        "Scrm Plys": "scrimmage_plays",
    }

    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        img = cells[0].find("img", src=LOGO_CODE_RE)
        if img is None:
            continue
        code = LOGO_CODE_RE.search(img["src"]).group(1)
        team_id = LOGO_CODE_TO_TEAM_ID.get(code, code)

        row = {"team_id": team_id}
        for i, header in enumerate(header_cells, start=0):
            if header not in col_map:
                continue
            row[col_map[header]] = cells[i].get_text(strip=True) if i < len(cells) else ""
        rows.append(row)

    return rows


def write_csv(rows: list[dict], path: Path):
    if not rows:
        print(f"  [AVISO] nenhuma linha para {path.name}, arquivo não gerado", file=sys.stderr)
        return
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
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    print(f"Extraindo downs (por time, ano={args.year})...")
    downs_rows = fetch_team_downs(args.year, session)
    downs_path = out_dir / f"downs_{args.year}.csv"
    write_csv(downs_rows, downs_path)
    print(f"  -> {len(downs_rows)} times salvos em {downs_path}")

    if not downs_rows:
        print(
            "\n[AVISO] downs veio com ZERO linhas nesta execução. O CSV não foi "
            "gravado — transform_stats.py trata isso como \"arquivo ausente\" e não "
            "quebra o pipeline, mas downs fica sem atualização até a próxima execução "
            "em que a página voltar a responder. Vale conferir a URL manualmente no "
            "navegador se isso persistir.",
            file=sys.stderr,
        )

    print("\nConcluído.")


if __name__ == "__main__":
    main()