"""
extract_player_game_logs.py

Fase 2 (consolidada) da extração: substitui extract_qb_games.py,
extract_extra_points.py e extract_defense_stats.py por um único scraper
que visita, uma vez por jogador, a mesma página de Logs da temporada
(nfl.com/players/<slug>/stats/logs/<ano>/) que os três scripts antigos já
usavam cada um separadamente — cada um olhando só pra um pedaço da mesma
tabela, com uma requisição HTTP própria.

POR QUE ISSO MUDOU (gap de "extração acumulada" da documentação do
projeto): extract_extra_points.py e extract_defense_stats.py, apesar de
já lerem uma página jogo-a-jogo (cada linha é UM jogo — é por isso que
dava pra somar as linhas e bater com o total real da temporada), jogavam
fora a granularidade semanal de propósito: somavam TODAS as semanas num
único valor de "total da temporada" por jogador, sem nunca gravar o WK de
cada linha individual. Isso obrigava transform_stats.py a ADIVINHAR sob
qual semana gravar esse total agregado (via team_weeks) — funciona, mas é
uma inferência, não um fato extraído. Pior: rodar o pipeline do zero
depois que várias semanas já passaram nunca reconstrói o histórico
semana-a-semana dessas duas tabelas, porque cada execução só sabia
escrever "o total até agora", sobrescrevendo o que veio antes.

Este script para de somar: cada linha da tabela de Logs vira uma linha
própria no CSV de saída, com a semana (`week`) que a própria tabela já
informa — sem nenhuma inferência. Isso finalmente aproveita a coluna WK
que a tabela sempre teve, e bate com o desenho do banco (kicking/defense
já têm `week` na chave primária).

GAP QUE CONTINUA EM ABERTO: passing, rushing, receiving, field-goals,
kickoffs, punting, kick_return e punt_return continuam vindo da página de
líderes por categoria (extract_category_stats.py), que é cumulativa/sem
semana — o mesmo problema que games/extra_points/defense tinham antes
desta mudança. A migração dessas categorias pra esta mesma fonte
semana-a-semana é o próximo passo natural (a página de Logs de um QB /
kicker / punter provavelmente já traz as colunas de passing / field goals
& kickoffs / punting na mesma tabela, do jeito que já vimos acontecer com
XP e tackles) — mas os nomes exatos de coluna dessas categorias na página
de Logs nunca foram conferidos contra uma página real (ver seção 9 da
documentação: todo mapeamento de coluna deste projeto precisou de uma
rodada de conferência contra o site ao vivo antes de ir pra produção).
Por isso este script NÃO tenta adivinhar esses nomes.

BÔNUS incluído mesmo assim: como o script já está, de qualquer forma, na
página de cada QB/kicker/defensor, ele também grava TODA coluna adicional
que aparecer na mesma tabela (captura genérica, igual normalize_header de
extract_category_stats.py) num CSV separado,
player_game_logs_raw_<ano>.csv. Isso não é consumido por transform_stats.py
ainda — serve só como ponto de partida pra quem for validar e migrar
passing/kicking(FG+KO)/etc no futuro: abra esse CSV, compare com uma
página de exemplo ao vivo, e escreva o rename map em transform_stats.py
do jeito que já existe pras colunas já verificadas.

ESCOPO DE REQUISIÇÕES: mantém exatamente o mesmo universo de jogadores
que os três scripts antigos cobriam juntos (QBs do roster, kickers,
posições defensivas) — não expande pra WR/RB/TE/punter, pra não inflar o
número de requisições sem necessidade nesta rodada. Games agora é
deduplicado (por time+semana) entre TODOS esses jogadores, não só QBs,
como uma rede de segurança extra caso um time fique sem QB ativo.

Entrada: o CSV gerado por extract_players_roster.py.

Saídas (em --output-dir):
    games_<ano>.csv          — mesmo formato de sempre (extract_qb_games.py)
    extra_points_<ano>.csv   — NOVO formato: uma linha por SEMANA (antes: uma
                                linha por kicker com o total da temporada)
    defense_<ano>.csv        — NOVO formato: uma linha por SEMANA (antes: uma
                                linha por jogador com o total da temporada)
    player_game_logs_raw_<ano>.csv — bônus, não usado ainda pelo transform_stats.py

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

RESULT_RE = re.compile(r"^([WLT])\s+(\d+)\s*-\s*(\d+)$")


def normalize_header(text: str) -> str:
    """Mesma normalização de extract_category_stats.py — usada só na
    captura genérica (raw), pra não depender de nome de coluna exato."""
    text = text.strip().lower()
    text = text.replace("%", "pct").replace("+", "plus").replace("/", "_per_")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def load_target_players(roster_csv: Path) -> list[dict]:
    """QBs + kickers + posições defensivas — o mesmo universo que
    extract_qb_games.py + extract_extra_points.py + extract_defense_stats.py
    cobriam juntos, cada um agora numa única passada por jogador."""
    players = []
    with open(roster_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pos = (row.get("position") or "").strip().upper()
            if pos == "QB" or pos == "K" or pos in DEFENSIVE_POSITIONS:
                players.append({
                    "player_id": row["player_id"],
                    "team_id": row["team_id"],
                    "position": pos,
                })
    return players


def find_regular_season_table(soup: BeautifulSoup):
    """Mesmo cuidado dos scripts anteriores: buscar o cabeçalho exato
    'Regular Season' (maiúsculas) em tag de heading (h1-h4), não em texto
    solto — o seletor de temporada no topo da página usa 'Regular season'
    minúsculo e apareceria antes numa busca case-insensitive."""
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
    return int(text) if text.isdigit() else 0


def to_number(text: str) -> float:
    text = (text or "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def fetch_player_game_log(player_id: str, team_id: str, position: str, year: int,
                           session: requests.Session) -> dict:
    """Uma única requisição por jogador. Retorna um dict com até 3 listas
    de linhas (games, extra_points, defense) + a captura genérica (raw),
    todas alinhadas linha-a-linha com a mesma tabela de Logs."""
    result = {"games": [], "extra_points": [], "defense": [], "raw": [], "status": "ok"}

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
    header_raw = [th.get_text(strip=True) for th in header_th]       # case original, p/ colunas já verificadas
    header_lower = [h.lower() for h in header_raw]                    # p/ WK/OPP/RESULT (extract_qb_games.py já usava minúsculo)
    header_norm = [normalize_header(h) for h in header_raw]           # p/ captura genérica (raw)

    try:
        idx_wk = header_lower.index("wk")
    except ValueError:
        print(f"  [AVISO] coluna 'WK' não encontrada para {player_id}, pulando", file=sys.stderr)
        result["status"] = "no_wk"
        return result
    idx_opp = header_lower.index("opp") if "opp" in header_lower else None
    idx_result = header_lower.index("result") if "result" in header_lower else None

    # colunas de Extra Point — mesmo cuidado de extract_extra_points.py: a
    # leitura é por posição relativa ao cabeçalho "XP Att" (há uma outra
    # coluna "BLK" no início da tabela, de field goal bloqueado — essa não
    # é a que queremos).
    idx_xp_att = header_raw.index("XP Att") if "XP Att" in header_raw else None
    idx_xpm = idx_xp_att + 1 if idx_xp_att is not None else None
    idx_xp_blk = idx_xp_att + 3 if idx_xp_att is not None else None

    # colunas de Defense — mesmos nomes de extract_defense_stats.py.
    defense_col_map = {
        "combined_tackles": "Total", "solo_tackles": "Solo", "assisted_tackles": "AST",
        "sacks": "SCK", "safeties": "SFTY", "pass_defended": "PDEF",
    }
    defense_idx = {k: header_raw.index(v) for k, v in defense_col_map.items() if v in header_raw}
    has_defense_cols = len(defense_idx) == len(defense_col_map)

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

        # --- games (só se a tabela tiver OPP + RESULT, como no QB log) ---
        if idx_opp is not None and idx_result is not None and len(cells) > max(idx_opp, idx_result):
            opponent, home_away = parse_opponent(cells[idx_opp].get_text(strip=True))
            win_loss, made_points, suffered_points = parse_result(cells[idx_result].get_text(strip=True))
            if win_loss is not None:
                result["games"].append({
                    "team_id": team_id, "season": year, "week": week,
                    "opponent": opponent, "home_away": home_away, "win_loss": win_loss,
                    "made_points": made_points, "suffered_points": suffered_points,
                })

        # --- extra points (uma linha por semana, não somado) ---
        if idx_xp_att is not None and len(cells) > idx_xp_blk:
            att = to_int(cells[idx_xp_att].get_text(strip=True))
            made = to_int(cells[idx_xpm].get_text(strip=True))
            blk = to_int(cells[idx_xp_blk].get_text(strip=True))
            pct = round(100 * made / att, 1) if att > 0 else 0
            result["extra_points"].append({
                "player_id_team": f"{player_id}-{team_id}", "season": year, "week": week,
                "extra_point_attempts": att, "extra_points_made": made,
                "extra_point_pct": pct, "extra_points_blocked": blk,
            })

        # --- defense (uma linha por semana, não somado) ---
        if has_defense_cols and len(cells) > max(defense_idx.values()):
            row = {"player_id_team": f"{player_id}-{team_id}", "season": year, "week": week}
            for key, i in defense_idx.items():
                row[key] = to_number(cells[i].get_text(strip=True))
            row["combined_tackles"] = int(row["combined_tackles"])
            row["solo_tackles"] = int(row["solo_tackles"])
            row["assisted_tackles"] = int(row["assisted_tackles"])
            row["safeties"] = int(row["safeties"])
            row["pass_defended"] = int(row["pass_defended"])
            # sacks fica float (pode ser 0.5)
            result["defense"].append(row)

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
    print(f"{len(players)} jogadores alvo (QB + K + defensivos) encontrados no roster.")
    if not players:
        print("[ERRO] Nenhum jogador alvo encontrado no roster — verifique se "
              f"{args.roster} não veio vazio ou sem a coluna 'position' preenchida "
              "(ver step anterior, extract_players_roster.py).", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    all_games = []
    all_extra_points = []
    all_defense = []
    all_raw = []
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
            all_games.append(g)

        all_extra_points.extend(data["extra_points"])
        all_defense.extend(data["defense"])
        all_raw.extend(data["raw"])

        time.sleep(args.delay)

    if not all_games:
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
            f"\n[AVISO] Execução concluiu com {len(all_games)} jogos coletados, mas "
            f"{request_errors} falha(s) de rede, {no_heading_count} página(s) sem "
            f"cabeçalho e {no_wk_count} tabela(s) sem coluna WK — alguns jogadores podem "
            "estar faltando nos CSVs de saída.",
            file=sys.stderr,
        )

    all_games.sort(key=lambda g: (g["team_id"], g["week"]))
    all_extra_points.sort(key=lambda r: (r["player_id_team"], r["week"]))
    all_defense.sort(key=lambda r: (r["player_id_team"], r["week"]))

    write_csv(all_games, out_dir / f"games_{args.year}.csv",
              fieldnames=["team_id", "season", "week", "opponent", "home_away",
                          "win_loss", "made_points", "suffered_points"])
    write_csv(all_extra_points, out_dir / f"extra_points_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "extra_point_attempts",
                          "extra_points_made", "extra_point_pct", "extra_points_blocked"])
    write_csv(all_defense, out_dir / f"defense_{args.year}.csv",
              fieldnames=["player_id_team", "season", "week", "combined_tackles",
                          "solo_tackles", "assisted_tackles", "sacks", "safeties", "pass_defended"])
    write_csv(all_raw, out_dir / f"player_game_logs_raw_{args.year}.csv")  # colunas dinâmicas

    print(f"\nConcluído: {len(all_games)} jogos, {len(all_extra_points)} linhas de extra point, "
          f"{len(all_defense)} linhas de defense, {len(all_raw)} linhas raw (bônus) salvas em {out_dir}/")


if __name__ == "__main__":
    main()