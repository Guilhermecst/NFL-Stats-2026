"""
extract_player_game_logs.py

Visita, uma vez por jogador, a página de Logs da temporada
(nfl.com/players/<slug>/stats/logs/<ano>/) e extrai as estatísticas
semana a semana: jogos, passing, rushing, receiving, kicking (field goals
+ kickoffs), punting, extra points e defense (tackles, interceptions,
fumbles forçados/recuperados).

A página lista uma tabela por jogador com uma linha por semana realmente
disputada (semanas de bye ficam ausentes), sem paginação. As colunas
variam de bloco em bloco conforme a posição do jogador: um QB tem um
bloco de passing seguido de um de rushing; um RB/WR/TE tem rushing e/ou
receiving; um kicker tem field goals e kickoffs; um punter tem punting;
um jogador defensivo tem o bloco de tackles/interceptions/fumbles. Vários
desses blocos reaproveitam os mesmos nomes de coluna (Att, Yds, TD
aparecem em mais de um bloco), então locate_blocks() localiza cada bloco
pela posição relativa dentro do cabeçalho, não por um nome isolado.

A página não tem nenhuma coluna de kickoff/punt return — nem para um
retornador titular de verdade. O par FUM/LOST de rushing/receiving também
vem como um único valor por semana, sem distinguir se o fumble aconteceu
numa corrida ou numa recepção: quando a linha tem produção de recepção
naquela semana, o par vai para receiving.fumbles; senão, para
rushing.fumbles.

Entrada: o CSV gerado por extract_players_roster.py.

Saídas (em --output-dir):
    games_<ano>.csv
    extra_points_<ano>.csv
    defense_<ano>.csv        — tackles, interceptions, fumbles forçados/recuperados
    passing_<ano>.csv
    rushing_<ano>.csv
    receiving_<ano>.csv
    kicking_fg_ko_<ano>.csv  — field goals + kickoffs
    punting_<ano>.csv
    player_game_logs_raw_<ano>.csv — todas as colunas cruas, sem mapear

Uso:
    python extract_player_game_logs.py --roster players_roster.csv --year 2026 \
        --output-dir ./logs_2026
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
    "DB", "FS", "CB", "S", "SAF", "SS", "LB", "DE", "OLB", "MLB", "ILB", "DT", "NT", "DL",
}
# Posições que geram alguma das tabelas de estatística individual desta
# extração. Deliberadamente fora: linha ofensiva (OL/T/G/C) — não produz
# nenhuma das tabelas alvo.
OFFENSE_SKILL_POSITIONS = {"RB", "FB", "WR", "TE"}

RESULT_RE = re.compile(r"^([WLT])\s+(\d+)\s*-\s*(\d+)$")


def normalize_header(text: str) -> str:
    """Normalização usada só na captura genérica (raw), pra não depender
    de nome de coluna exato."""
    text = text.strip().lower()
    text = text.replace("%", "pct").replace("+", "plus").replace("/", "_per_")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def load_target_players(roster_csv: Path) -> list[dict]:
    """QB + RB/FB/WR/TE + K + P + posições defensivas — todo mundo que
    pode gerar alguma das tabelas de estatística individual."""
    players = []
    with open(roster_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pos = (row.get("position") or "").strip().upper()
            if (pos == "QB" or pos == "K" or pos == "P"
                    or pos in OFFENSE_SKILL_POSITIONS or pos in DEFENSIVE_POSITIONS):
                players.append({
                    "player_id": row["player_id"],
                    "team_id": row["team_id"],
                    "position": pos,
                })
    return players


def find_regular_season_table(soup: BeautifulSoup):
    """Busca o cabeçalho exato 'Regular Season' (maiúsculas) em tag de
    heading (h1-h4), não em texto solto — o seletor de temporada no topo
    da página usa 'Regular season' minúsculo e apareceria antes numa
    busca case-insensitive."""
    heading = None
    for tag in soup.find_all(re.compile(r"^h[1-4]$")):
        if tag.get_text(strip=True) == "Regular Season":
            heading = tag
            break
    if heading is None:
        return None
    return heading.find_next("table")


def parse_opponent(opp_cell: str):
    opp_cell = opp_cell.strip()
    if opp_cell.startswith("@"):
        return opp_cell[1:].strip(), "Away"
    return opp_cell, "Home"


def parse_result(result_cell: str):
    m = RESULT_RE.match(result_cell.strip())
    if not m:
        return None, None, None
    return m.group(1), int(m.group(2)), int(m.group(3))


def to_int(text: str) -> int:
    text = (text or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def to_number(text: str) -> float:
    text = (text or "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def locate_blocks(header_raw: list[str], header_lower: list[str]) -> dict:
    """Varre o cabeçalho UMA VEZ (é o mesmo pra todas as linhas de um
    jogador) e localiza, por CONTEÚDO/POSIÇÃO relativa (não por um nome
    isolado — 'Att'/'Yds'/'TD' se repetem em mais de um bloco na mesma
    tabela), onde começa cada bloco de estatística. Retorna um dict
    {nome_do_bloco: índice_inicial}; blocos ausentes simplesmente não
    aparecem no dict — não é erro, é o jogador não ter aquele tipo de
    produção na página dele.

    Blocos reconhecidos:
      passing   (9 cols, QB): Comp, Att, Yds, Avg, TD, Int, Sck, SckY, Rate
      rushing_qb  (4 cols, dentro da página de QB, sem Lng): Att, Yds, Avg, TD
      rushing_full(5 cols, RB/WR/TE/FB): Att, Yds, Avg, Lng, TD
      receiving (5 cols): Rec, Yds, Avg, Lng, TD
      fum_lost  (2 cols, trailer único pra corrida+recepção): Fum, Lost
      field_goals (5 cols, kicker): Blk, Lng, FG Att, FGM, Pct
      kickoffs    (5 cols, kicker): KO, Avg, TB, Ret, Avg
      punting     (15 cols, punter): Punts, Yds, Net Yds, Lng, Avg, Net Avg,
                  Blk, OOB, Dn, In 20, TB, FC, Ret, RetY, TD
    """
    blocks = {}
    n = len(header_lower)

    idx_comp = header_lower.index("comp") if "comp" in header_lower else None
    if idx_comp is not None and idx_comp + 8 < n:
        blocks["passing"] = idx_comp

    search_from = (idx_comp + 9) if idx_comp is not None else 0
    for j in range(search_from, n):
        if header_lower[j] == "att":
            if j + 3 < n and header_lower[j + 3] == "lng":
                blocks["rushing_full"] = j
            elif j + 3 < n and header_lower[j + 3] == "td":
                blocks["rushing_qb"] = j
            break

    idx_rec = header_lower.index("rec") if "rec" in header_lower else None
    if idx_rec is not None and idx_rec + 4 < n:
        blocks["receiving"] = idx_rec

    idx_fum = header_lower.index("fum") if "fum" in header_lower else None
    if idx_fum is not None and idx_fum + 1 < n and header_lower[idx_fum + 1] == "lost":
        blocks["fum_lost"] = idx_fum

    idx_fg_att = header_lower.index("fg att") if "fg att" in header_lower else None
    if idx_fg_att is not None and idx_fg_att - 2 >= 0 and idx_fg_att + 2 < n:
        blocks["field_goals"] = idx_fg_att - 2  # Blk, Lng, FG Att, FGM, Pct

    idx_ko = header_lower.index("ko") if "ko" in header_lower else None
    if idx_ko is not None and idx_ko + 4 < n:
        blocks["kickoffs"] = idx_ko

    idx_punts = header_lower.index("punts") if "punts" in header_lower else None
    if idx_punts is not None and idx_punts + 14 < n:
        blocks["punting"] = idx_punts

    return blocks


def cell_at(cells, i) -> str:
    return cells[i].get_text(strip=True) if i is not None and i < len(cells) else ""


def fetch_player_game_log(player_id: str, team_id: str, position: str, year: int,
                           session: requests.Session) -> dict:
    """Uma única requisição por jogador. Retorna um dict com listas de
    linhas por tabela de destino + a captura genérica (raw), todas
    alinhadas linha-a-linha com a mesma tabela de Logs."""
    result = {
        "games": [], "extra_points": [], "defense": [],
        "passing": [], "rushing": [], "receiving": [],
        "kicking_fg_ko": [], "punting": [],
        "raw": [], "status": "ok",
    }

    url = f"https://www.nfl.com/players/{player_id}/stats/logs/{year}/"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = find_regular_season_table(soup)
    if table is None:
        print(f"  [AVISO] cabeçalho 'Regular Season' não encontrado para {player_id}", file=sys.stderr)
        result["status"] = "no_heading"
        return result

    header_th = table.find("thead").find_all("th") if table.find("thead") else []
    header_raw = [th.get_text(strip=True) for th in header_th]       # case original, p/ colunas de nome fixo (WK/OPP/RESULT/XP/defense)
    header_lower = [h.lower() for h in header_raw]                    # p/ blocos localizados por posição relativa
    header_norm = [normalize_header(h) for h in header_raw]           # p/ captura genérica (raw)

    try:
        idx_wk = header_lower.index("wk")
    except ValueError:
        print(f"  [AVISO] coluna 'WK' não encontrada para {player_id}, pulando", file=sys.stderr)
        result["status"] = "no_wk"
        return result
    idx_opp = header_lower.index("opp") if "opp" in header_lower else None
    idx_result = header_lower.index("result") if "result" in header_lower else None

    # colunas de Extra Point — lidas por posição relativa ao cabeçalho
    # exato "XP Att" (há outra coluna "Blk" no início da tabela, de field
    # goal bloqueado — não é esta).
    idx_xp_att = header_raw.index("XP Att") if "XP Att" in header_raw else None
    idx_xpm = idx_xp_att + 1 if idx_xp_att is not None else None
    idx_xp_blk = idx_xp_att + 3 if idx_xp_att is not None else None

    # colunas de Defense: tackles, interceptions (com jardas/retorno mais
    # longo/touchdown de retorno) e fumbles forçados/recuperados, todas no
    # mesmo bloco de cabeçalho fixo.
    defense_col_map = {
        "combined_tackles": "Total", "solo_tackles": "Solo", "assisted_tackles": "AST",
        "sacks": "SCK", "safeties": "SFTY", "pass_defended": "PDEF",
        "interceptions": "INT", "interception_yards": "YDS", "interception_long": "LNG",
        "interception_touchdowns": "TDS", "fumbles_forced": "FF", "fumbles_recovered": "FR",
    }
    defense_idx = {k: header_raw.index(v) for k, v in defense_col_map.items() if v in header_raw}
    has_defense_cols = len(defense_idx) == len(defense_col_map)

    # blocos localizados por posição relativa (ver locate_blocks).
    blocks = locate_blocks(header_raw, header_lower)

    body = table.find("tbody") or table
    seen_own_weeks = set()  # (week) já visto NESTE jogador — evita duplicar se o layout repetir uma linha
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) <= idx_wk:
            continue
        week_text = cells[idx_wk].get_text(strip=True)
        if not week_text.isdigit():
            continue
        week = int(week_text)
        if week in seen_own_weeks:
            continue
        seen_own_weeks.add(week)

        player_id_team = f"{player_id}-{team_id}"
        base = {"player_id_team": player_id_team, "season": year, "week": week}

        # --- games (só se a tabela tiver OPP + RESULT) ---
        if idx_opp is not None and idx_result is not None and len(cells) > max(idx_opp, idx_result):
            opponent, home_away = parse_opponent(cells[idx_opp].get_text(strip=True))
            win_loss, made_points, suffered_points = parse_result(cells[idx_result].get_text(strip=True))
            if win_loss is not None:
                result["games"].append({
                    "team_id": team_id, "season": year, "week": week,
                    "opponent": opponent, "home_away": home_away, "win_loss": win_loss,
                    "made_points": made_points, "suffered_points": suffered_points,
                })

        # --- extra points ---
        if idx_xp_att is not None and len(cells) > idx_xp_blk:
            att = to_int(cell_at(cells, idx_xp_att))
            made = to_int(cell_at(cells, idx_xpm))
            blk = to_int(cell_at(cells, idx_xp_blk))
            pct = round(100 * made / att, 1) if att > 0 else 0
            result["extra_points"].append({
                **base, "extra_point_attempts": att, "extra_points_made": made,
                "extra_point_pct": pct, "extra_points_blocked": blk,
            })

        # --- defense: tackles, interceptions, fumbles forçados/recuperados ---
        if has_defense_cols and len(cells) > max(defense_idx.values()):
            row = dict(base)
            for key, i in defense_idx.items():
                row[key] = to_number(cell_at(cells, i))
            row["combined_tackles"] = int(row["combined_tackles"])
            row["solo_tackles"] = int(row["solo_tackles"])
            row["assisted_tackles"] = int(row["assisted_tackles"])
            row["safeties"] = int(row["safeties"])
            row["pass_defended"] = int(row["pass_defended"])
            row["interceptions"] = int(row["interceptions"])
            row["interception_yards"] = int(row["interception_yards"])
            row["interception_long"] = int(row["interception_long"])
            row["interception_touchdowns"] = int(row["interception_touchdowns"])
            row["fumbles_forced"] = int(row["fumbles_forced"])
            row["fumbles_recovered"] = int(row["fumbles_recovered"])
            # sacks fica float (pode ser 0.5)
            result["defense"].append(row)

        # --- passing ---
        if "passing" in blocks and len(cells) > blocks["passing"] + 8:
            i = blocks["passing"]
            comp = to_int(cell_at(cells, i))
            att = to_int(cell_at(cells, i + 1))
            yds = to_int(cell_at(cells, i + 2))
            result["passing"].append({
                **base,
                "completions": comp, "attempts": att, "yards": yds,
                "yards_per_attempt": to_number(cell_at(cells, i + 3)),
                "touchdowns": to_int(cell_at(cells, i + 4)),
                "interceptions": to_int(cell_at(cells, i + 5)),
                "sacks": to_int(cell_at(cells, i + 6)),
                "sacks_yards": to_int(cell_at(cells, i + 7)),
                "rate": to_number(cell_at(cells, i + 8)),
                "completion_pct": round(100 * comp / att, 1) if att > 0 else 0,
            })

        # --- rushing (2 variantes de largura, ver locate_blocks) ---
        rush_idx = blocks.get("rushing_full") if "rushing_full" in blocks else blocks.get("rushing_qb")
        if rush_idx is not None:
            width = 5 if "rushing_full" in blocks else 4
            if len(cells) > rush_idx + width - 1:
                row = {
                    **base,
                    "attempts": to_int(cell_at(cells, rush_idx)),
                    "yards": to_int(cell_at(cells, rush_idx + 1)),
                    "touchdowns": to_int(cell_at(cells, rush_idx + width - 1)),
                }
                if width == 5:
                    row["long_gain"] = to_int(cell_at(cells, rush_idx + 3))
                result["rushing"].append(row)

        # --- receiving ---
        if "receiving" in blocks and len(cells) > blocks["receiving"] + 4:
            i = blocks["receiving"]
            result["receiving"].append({
                **base,
                "receptions": to_int(cell_at(cells, i)),
                "yards": to_int(cell_at(cells, i + 1)),
                "long_gain": to_int(cell_at(cells, i + 3)),
                "touchdowns": to_int(cell_at(cells, i + 4)),
            })

        # --- fumbles cometidos (rushing/receiving): único par FUM/LOST por
        # linha, sem distinguir corrida vs recepção — atribuído a
        # receiving quando há bloco de recepção na mesma semana, senão a
        # rushing.
        if "fum_lost" in blocks and len(cells) > blocks["fum_lost"] + 1:
            fum = to_int(cell_at(cells, blocks["fum_lost"]))
            if result["receiving"] and result["receiving"][-1]["week"] == week:
                result["receiving"][-1]["fumbles"] = fum
            elif result["rushing"] and result["rushing"][-1]["week"] == week:
                result["rushing"][-1]["fumbles"] = fum

        # --- field goals ---
        if "field_goals" in blocks and len(cells) > blocks["field_goals"] + 4:
            i = blocks["field_goals"]
            result["kicking_fg_ko"].append({
                **base,
                "field_goals_blocked": to_int(cell_at(cells, i)),
                "field_goal_long": to_int(cell_at(cells, i + 1)),
                "field_goal_attempts": to_int(cell_at(cells, i + 2)),
                "field_goals_made": to_int(cell_at(cells, i + 3)),
                "field_goal_pct": to_number(cell_at(cells, i + 4)),
            })

        # --- kickoffs (mescla na mesma linha de FG se já existir) ---
        if "kickoffs" in blocks and len(cells) > blocks["kickoffs"] + 4:
            i = blocks["kickoffs"]
            ko_row = {
                "kickoffs": to_int(cell_at(cells, i)),
                "kickoff_avg": to_number(cell_at(cells, i + 1)),
                "touchbacks": to_int(cell_at(cells, i + 2)),
                "kickoff_returns_against": to_int(cell_at(cells, i + 3)),
                "kickoff_return_avg_against": to_number(cell_at(cells, i + 4)),
            }
            if result["kicking_fg_ko"] and result["kicking_fg_ko"][-1]["week"] == week:
                result["kicking_fg_ko"][-1].update(ko_row)
            else:
                result["kicking_fg_ko"].append({**base, **ko_row})

        # --- punting ---
        if "punting" in blocks and len(cells) > blocks["punting"] + 14:
            i = blocks["punting"]
            result["punting"].append({
                **base,
                "punts": to_int(cell_at(cells, i)),
                "yards": to_int(cell_at(cells, i + 1)),
                "net_yards": to_int(cell_at(cells, i + 2)),
                "long_gain": to_int(cell_at(cells, i + 3)),
                "average": to_number(cell_at(cells, i + 4)),
                "net_average": to_number(cell_at(cells, i + 5)),
                "blocked": to_int(cell_at(cells, i + 6)),
                "out_of_bounds": to_int(cell_at(cells, i + 7)),
                "downed": to_int(cell_at(cells, i + 8)),
                "in_20_yards_line": to_int(cell_at(cells, i + 9)),
                "touchbacks": to_int(cell_at(cells, i + 10)),
                "fair_catches_against": to_int(cell_at(cells, i + 11)),
                "returns_against": to_int(cell_at(cells, i + 12)),
                "return_yards_against": to_int(cell_at(cells, i + 13)),
                "return_touchdowns_against": to_int(cell_at(cells, i + 14)),
            })

        # --- captura genérica (raw) — bônus, todas as colunas, sem mapear ---
        raw_row = {
            "player_id": player_id, "team_id": team_id, "position": position,
            "season": year, "week": week,
        }
        for i, col in enumerate(header_norm):
            if i < len(cells) and col:
                raw_row[col] = cells[i].get_text(strip=True)
        result["raw"].append(raw_row)

    return result


def write_csv(rows: list[dict], path: Path, fieldnames: list[str] = None):
    if not rows:
        print(f"  [AVISO] nenhuma linha para {path.name}, arquivo não gerado", file=sys.stderr)
        return
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
        for row in rows[1:]:
            for k in row.keys():
                if k not in fieldnames:
                    fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--roster", required=True, help="CSV gerado por extract_players_roster.py")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output-dir", default="./logs_output")
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    players = load_target_players(Path(args.roster))
    print(f"{len(players)} jogadores alvo (QB + RB/FB/WR/TE + K + P + defensivos) encontrados no roster.")
    if not players:
        print("[ERRO] Nenhum jogador alvo encontrado no roster — verifique se "
              f"{args.roster} não veio vazio ou sem a coluna 'position' preenchida "
              "(ver step anterior, extract_players_roster.py).", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    collected = {
        "games": [], "extra_points": [], "defense": [],
        "passing": [], "rushing": [], "receiving": [],
        "kicking_fg_ko": [], "punting": [], "raw": [],
    }
    seen_games = set()  # (team_id, week) já coletado, dedup entre jogadores do mesmo time
    request_errors = 0
    no_heading_count = 0
    no_wk_count = 0

    for i, player in enumerate(players, start=1):
        player_id, team_id, position = player["player_id"], player["team_id"], player["position"]
        print(f"[{i}/{len(players)}] {player_id} ({team_id}, {position})...")
        try:
            data = fetch_player_game_log(player_id, team_id, position, args.year, session)
        except requests.RequestException as e:
            print(f"  [ERRO] falha ao buscar {player_id}: {e}", file=sys.stderr)
            request_errors += 1
            continue

        if data["status"] == "no_heading":
            no_heading_count += 1
        elif data["status"] == "no_wk":
            no_wk_count += 1

        for g in data["games"]:
            key = (g["team_id"], g["week"])
            if key in seen_games:
                continue
            seen_games.add(key)
            collected["games"].append(g)

        for key in ("extra_points", "defense", "passing", "rushing", "receiving",
                    "kicking_fg_ko", "punting", "raw"):
            collected[key].extend(data[key])

        time.sleep(args.delay)

    if not collected["games"]:
        print(
            "\n[ERRO] Zero jogos coletados de "
            f"{len(players)} jogador(es) alvo — abortando sem gravar nenhum CSV em "
            f"{out_dir}/, para não deixar transform_stats.py falhar mais adiante com um "
            "FileNotFoundError sem contexto.\n"
            f"  {request_errors} falha(s) de requisição HTTP\n"
            f"  {no_heading_count} página(s) sem o cabeçalho 'Regular Season' "
            "(layout do nfl.com pode ter mudado, ou bloqueio/rate-limit disfarçado de "
            "página vazia)\n"
            f"  {no_wk_count} tabela(s) encontrada(s) mas sem coluna 'WK' reconhecida\n"
            "Se os três contadores acima estiverem todos zerados mas mesmo assim chegou "
            "aqui, o problema está na dedupe (team_id, week) — improvável, mas confira "
            "seen_games antes de investigar o nfl.com.",
            file=sys.stderr,
        )
        sys.exit(1)

    if request_errors or no_heading_count or no_wk_count:
        print(
            f"\n[AVISO] Execução concluiu com {len(collected['games'])} jogos coletados, mas "
            f"{request_errors} falha(s) de rede, {no_heading_count} página(s) sem "
            f"cabeçalho e {no_wk_count} tabela(s) sem coluna WK — alguns jogadores podem "
            "estar faltando nos CSVs de saída.",
            file=sys.stderr,
        )

    for key in ("passing", "rushing", "receiving", "kicking_fg_ko", "punting"):
        if not collected[key]:
            print(
                f"[AVISO] Nenhuma linha coletada para '{key}' — confira se locate_blocks() "
                "ainda reconhece o layout real da página (colunas confirmadas por "
                "amostragem, não garantidas para sempre).",
                file=sys.stderr,
            )

    collected["games"].sort(key=lambda g: (g["team_id"], g["week"]))
    for key in ("extra_points", "defense", "passing", "rushing", "receiving",
                "kicking_fg_ko", "punting"):
        collected[key].sort(key=lambda r: (r["player_id_team"], r["week"]))

    write_csv(collected["games"], out_dir / f"games_{args.year}.csv",
              fieldnames=["team_id", "season", "week", "opponent", "home_away",
                          "win_loss", "made_points", "suffered_points"])
    write_csv(collected["extra_points"], out_dir / f"extra_points_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "extra_point_attempts",
                          "extra_points_made", "extra_point_pct", "extra_points_blocked"])
    write_csv(collected["defense"], out_dir / f"defense_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "combined_tackles",
                          "solo_tackles", "assisted_tackles", "sacks", "safeties",
                          "pass_defended", "interceptions", "interception_yards",
                          "interception_long", "interception_touchdowns",
                          "fumbles_forced", "fumbles_recovered"])
    write_csv(collected["passing"], out_dir / f"passing_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "completions", "attempts",
                          "yards", "yards_per_attempt", "completion_pct", "touchdowns",
                          "interceptions", "sacks", "sacks_yards", "rate"])
    write_csv(collected["rushing"], out_dir / f"rushing_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "attempts", "yards",
                          "long_gain", "touchdowns", "fumbles"])
    write_csv(collected["receiving"], out_dir / f"receiving_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "receptions", "yards",
                          "long_gain", "touchdowns", "fumbles"])
    write_csv(collected["kicking_fg_ko"], out_dir / f"kicking_fg_ko_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "field_goals_made",
                          "field_goal_attempts", "field_goal_pct", "field_goal_long",
                          "field_goals_blocked", "kickoffs", "kickoff_avg", "touchbacks",
                          "kickoff_returns_against", "kickoff_return_avg_against"])
    write_csv(collected["punting"], out_dir / f"punting_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "punts", "yards", "net_yards",
                          "long_gain", "average", "net_average", "blocked", "out_of_bounds",
                          "downed", "in_20_yards_line", "touchbacks", "fair_catches_against",
                          "returns_against", "return_yards_against", "return_touchdowns_against"])
    write_csv(collected["raw"], out_dir / f"player_game_logs_raw_{args.year}.csv")  # colunas dinâmicas

    print(
        f"\nConcluído: {len(collected['games'])} jogos, "
        f"{len(collected['passing'])} linhas de passing, "
        f"{len(collected['rushing'])} de rushing, "
        f"{len(collected['receiving'])} de receiving, "
        f"{len(collected['kicking_fg_ko'])} de kicking (FG+KO), "
        f"{len(collected['punting'])} de punting, "
        f"{len(collected['extra_points'])} de extra point, "
        f"{len(collected['defense'])} de defense, "
        f"{len(collected['raw'])} linhas raw (bônus) salvas em {out_dir}/"
    )


if __name__ == "__main__":
    main()