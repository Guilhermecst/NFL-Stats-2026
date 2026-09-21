"""
transform_stats.py

Fase 3 do pipeline: junta os CSVs gerados pelos scripts de extração
(roster, categorias de líderes que sobraram, logs por jogador) e produz
um CSV final por tabela do banco, já com as colunas renomeadas para bater
com o DDL e prontos para o script de upsert no Supabase.

Entradas esperadas (ajuste os caminhos via argumentos se necessário):
    players_roster.csv                       (extract_players_roster.py)
    logs_dir/games_<ano>.csv                 (extract_player_game_logs.py — semana real)
    logs_dir/extra_points_<ano>.csv          (idem)
    logs_dir/defense_<ano>.csv               (idem — tackles/sacks/safeties/PD)
    logs_dir/passing_<ano>.csv               (idem)
    logs_dir/rushing_<ano>.csv               (idem)
    logs_dir/receiving_<ano>.csv             (idem)
    logs_dir/kicking_fg_ko_<ano>.csv         (idem — field goals + kickoffs)
    logs_dir/punting_<ano>.csv               (idem)
    stats_dir/fumbles_<ano>.csv              (extract_category_stats.py — acumulado, sem semana)
    stats_dir/interceptions_<ano>.csv        (idem)
    stats_dir/kickoff_returns_<ano>.csv      (idem)
    stats_dir/punt_returns_<ano>.csv         (idem)
    stats_dir/downs_<ano>.csv                (extract_category_stats.py — tabela por time, sem `week`)

FONTE DE CADA TABELA: games, extra_points, defense (tackles), passing,
rushing, receiving, kicking (FG+KO+XP) e punting vêm de
extract_player_game_logs.py, com uma linha por semana real de cada
jogador. kick_return, punt_return, fumbles (forced/opponent-recovered) e
interceptions vêm de extract_category_stats.py: essas categorias trazem
o total acumulado da temporada por jogador (sem coluna de semana própria
na fonte), pois a página de Logs do nfl.com não expõe essas colunas para
nenhum jogador — cada linha é carimbada com a última semana realmente
disputada pelo time do jogador (ver determine_team_weeks()).

Cada tabela individual gera exatamente as colunas definidas no schema do
banco (ver MER) — colunas para as quais o nfl.com não disponibiliza dado
por semana simplesmente não são geradas.

GAP DE FONTE: nem a página de líderes nem a de Logs separam estatística
por time — um jogador negociado no meio da temporada aparece com o total
(ou a semana) sob o TIME ATUAL, mesmo que parte da produção tenha sido
pelo time anterior. O `player_id_team` é sempre montado com o time do
roster mais recente.

SEMANA POR TIME, NÃO GLOBAL (relevante para kick_return, punt_return,
fumbles e interceptions, que seguem cumulativas): como essas categorias
trazem o total ACUMULADO DA TEMPORADA por jogador, não um valor por
semana, o script precisa decidir sob qual "semana" gravar esse total.
Ele faz isso via determine_team_weeks(), que olha, TIME A TIME, qual foi
a última semana com jogo disputado no CSV de games desta mesma rodada —
não um único número de semana "global" (a maior de toda a liga). Isso
importa em execuções fora do dia de cron normal (ex: rodando no meio da
semana pra validar algo, enquanto só alguns times já jogaram): um
jogador de um time que ainda não jogou continua sendo gravado sob a
última semana REAL do próprio time (sobrescrevendo a mesma linha,
idempotente) até que o jogo dele apareça no log — em vez de criar uma
linha "semana N" espúria, idêntica à "semana N-1", só porque outro time
qualquer já jogou a rodada N.

LIMITAÇÃO DA FONTE — reexecução do zero não reconstrói o histórico
semana-a-semana de kick_return/punt_return/fumbles/interceptions: essas
tabelas ainda leem stats_dir/<categoria>_<ano>.csv, cumulativo sem
histórico por semana. A única forma de recuperar o histórico dessas
tabelas depois do fato é backup do banco ou os CSVs finais
(final_<ano>/*.csv) de execuções passadas.

Uso:
    python transform_stats.py --year 2026 \
        --roster players_roster.csv \
        --stats-dir ./stats_2026 \
        --logs-dir ./logs_2026 \
        --output-dir ./final_2026
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# sigla (team_id) -> apelido usado nas colunas "OPP" das páginas de logs
TEAM_NICKNAMES = {
    "BUF": "Bills", "MIA": "Dolphins", "NE": "Patriots", "NYJ": "Jets",
    "BAL": "Ravens", "CIN": "Bengals", "CLE": "Browns", "PIT": "Steelers",
    "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars", "TEN": "Titans",
    "DEN": "Broncos", "KC": "Chiefs", "LV": "Raiders", "LAC": "Chargers",
    "DAL": "Cowboys", "NYG": "Giants", "PHI": "Eagles", "WAS": "Commanders",
    "CHI": "Bears", "DET": "Lions", "GB": "Packers", "MIN": "Vikings",
    "ATL": "Falcons", "CAR": "Panthers", "NO": "Saints", "TB": "Buccaneers",
    "ARI": "Cardinals", "LAR": "Rams", "SF": "49ers", "SEA": "Seahawks",
}
NICKNAME_TO_TEAM_ID = {v: k for k, v in TEAM_NICKNAMES.items()}

# sigla (team_id) -> "<Conferência> <Divisão>", ex: "NFC East".
# Alinhamento de divisões da NFL (estático — só muda em realinhamentos
# raros da liga, não precisa ser re-extraído toda semana).
TEAM_CONF_DIV = {
    # AFC East
    "BUF": "AFC East", "MIA": "AFC East", "NE": "AFC East", "NYJ": "AFC East",
    # AFC North
    "BAL": "AFC North", "CIN": "AFC North", "CLE": "AFC North", "PIT": "AFC North",
    # AFC South
    "HOU": "AFC South", "IND": "AFC South", "JAX": "AFC South", "TEN": "AFC South",
    # AFC West
    "DEN": "AFC West", "KC": "AFC West", "LV": "AFC West", "LAC": "AFC West",
    # NFC East
    "DAL": "NFC East", "NYG": "NFC East", "PHI": "NFC East", "WAS": "NFC East",
    # NFC North
    "CHI": "NFC North", "DET": "NFC North", "GB": "NFC North", "MIN": "NFC North",
    # NFC South
    "ATL": "NFC South", "CAR": "NFC South", "NO": "NFC South", "TB": "NFC South",
    # NFC West
    "ARI": "NFC West", "LAR": "NFC West", "SF": "NFC West", "SEA": "NFC West",
}


def load_roster(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df["player_id_team"] = df["player_id"] + "-" + df["team_id"]
    return df


def determine_team_weeks(games_path: Path) -> dict[str, int]:
    """Retorna, POR TIME, a última semana com jogo já disputado no CSV de
    jogos desta rodada (extract_player_game_logs.py) — não um único número
    global.

    Por quê: kick_return, punt_return, fumbles e interceptions trazem o
    TOTAL ACUMULADO DA TEMPORADA de cada jogador, não um valor por semana.
    Se a execução acontece no meio da semana — nem todos os 32 times já
    jogaram — usar a MAIOR semana entre TODOS os times faz o script
    carimbar jogadores de times que ainda não jogaram com o número da
    semana seguinte, mesmo que o total deles ainda seja o da semana
    anterior (o jogador simplesmente ainda não jogou). Isso criaria uma
    linha nova "semana N" idêntica à "semana N-1", uma duplicata falsa no
    banco.

    Com o mapa por time, um jogador de um time que ainda não jogou a
    semana mais recente continua sendo gravado sob a última semana real
    do PRÓPRIO time (sobrescrevendo a mesma linha, idempotente) até que o
    jogo dele apareça no log — sem criar linha espúria."""
    df = pd.read_csv(games_path, dtype=str)
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df = df.dropna(subset=["week"])
    if df.empty:
        raise ValueError(f"Não foi possível determinar a semana atual a partir de {games_path}")
    return df.groupby("team_id")["week"].max().astype(int).to_dict()


def attach_player_id_team(df: pd.DataFrame, roster: pd.DataFrame, year: int,
                           team_weeks: dict[str, int]) -> pd.DataFrame:
    """Junta um CSV de categoria (só tem player_id) com o roster para
    obter team_id e montar player_id_team, e carimba cada linha com a
    semana do PRÓPRIO time do jogador (ver determine_team_weeks)."""
    # dedup na origem: paginação por cursor pode repetir uma linha quando
    # vários jogadores empatam no critério de ordenação na borda entre
    # páginas (extract_category_stats.py) — sem isso, o upsert falha com
    # "ON CONFLICT DO UPDATE command cannot affect row a second time".
    before = len(df)
    df = df.drop_duplicates(subset=["player_id"])
    if len(df) < before:
        print(f"  [AVISO] {before - len(df)} linha(s) duplicada(s) removida(s) "
              f"(provável sobreposição de paginação)", file=sys.stderr)

    lookup = roster[["player_id", "team_id", "player_id_team"]].drop_duplicates("player_id")
    merged = df.merge(lookup, on="player_id", how="left")

    missing = merged["team_id"].isna().sum()
    if missing:
        print(f"  [AVISO] {missing} jogador(es) não encontrados no roster "
              f"(aposentado/cortado?) — linhas descartadas", file=sys.stderr)
        merged = merged.dropna(subset=["team_id"])

    merged["season"] = year
    merged["week"] = merged["team_id"].map(team_weeks)
    no_week = merged["week"].isna().sum()
    if no_week:
        print(f"  [AVISO] {no_week} jogador(es) de time(s) sem nenhum jogo "
              f"registrado ainda em {list(merged.loc[merged['week'].isna(), 'team_id'].unique())} "
              f"— linhas descartadas", file=sys.stderr)
        merged = merged.dropna(subset=["week"])
    merged["week"] = merged["week"].astype(int)
    return merged


def split_att_made(df: pd.DataFrame, col: str, made_col: str, att_col: str) -> pd.DataFrame:
    """Converte uma coluna tipo '8/8' (feito/tentado) em duas colunas numéricas."""
    parts = df[col].fillna("0/0").str.split("/", expand=True)
    df[made_col] = pd.to_numeric(parts[0], errors="coerce").fillna(0).astype(int)
    df[att_col] = pd.to_numeric(parts[1], errors="coerce").fillna(0).astype(int)
    return df


def to_num(df: pd.DataFrame, cols: list[str]):
    """Converte para numérico decimal (float) — usar só nas colunas que
    são numeric()/decimal no banco."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)


def to_int(df: pd.DataFrame, cols: list[str]):
    """Converte para inteiro de verdade — usar nas colunas integer/smallint
    do banco. Precisa ser um passo separado de to_num() porque
    pd.to_numeric() sozinho gera float64 (ex: 6.0), e o Postgres rejeita
    '1.0' num campo integer (psycopg2.errors.InvalidTextRepresentation)."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)


def build_teams() -> pd.DataFrame:
    """Tabela `teams`, só a coluna `conf_div` (ex: "NFC East"). Não depende
    de nenhum CSV de extração — vem de TEAM_CONF_DIV, um mapa estático (a
    divisão de um time só muda em realinhamentos raros da liga). As demais
    colunas de `teams` (nome, cidade, logo, etc.) vêm do seed original e
    não são tocadas por este pipeline; o upsert em load_to_supabase.py
    atualiza só `conf_div` para cada team_id já existente."""
    rows = [{"team_id": team_id, "conf_div": conf_div} for team_id, conf_div in TEAM_CONF_DIV.items()]
    return pd.DataFrame(rows, columns=["team_id", "conf_div"])


def build_players(roster: pd.DataFrame, year: int) -> pd.DataFrame:
    """Tabela `players`: player_id_team, season e os dados básicos de cada
    jogador (id, nome, posição, time) vindos do roster."""
    out = roster.rename(columns={"position": "player_position"})[
        ["player_id_team", "player_id", "player_name", "player_position", "team_id"]
    ].copy()
    out["season"] = year
    return out[["player_id_team", "season", "player_id", "player_name", "player_position",
                "team_id"]]


def build_passing(passing_path: Path) -> pd.DataFrame:
    """Tabela `passing`: lê passing_<ano>.csv (extract_player_game_logs.py,
    uma linha por semana real) e converte os tipos para bater com o DDL."""
    cols = ["player_id_team", "season", "week", "yards", "yards_per_attempt", "attempts",
            "completions", "completion_pct", "touchdowns", "interceptions", "rate",
            "sacks", "sacks_yards"]
    if not passing_path.exists():
        print(f"  [AVISO] {passing_path} não encontrado — passing ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(passing_path, dtype=str)
    to_num(df, ["yards_per_attempt", "rate", "completion_pct"])
    to_int(df, ["season", "week", "yards", "attempts", "completions", "touchdowns",
                "interceptions", "sacks", "sacks_yards"])
    return df[cols]


def build_rushing(rushing_path: Path) -> pd.DataFrame:
    """Tabela `rushing`: lê rushing_<ano>.csv (extract_player_game_logs.py,
    uma linha por semana real). `fumbles` vem do par FUM/LOST único da
    página de Logs, atribuído a rushing quando a linha não tem bloco de
    recepção na mesma semana (ver extract_player_game_logs.py)."""
    cols = ["player_id_team", "season", "week", "yards", "attempts", "touchdowns",
            "long_gain", "fumbles"]
    if not rushing_path.exists():
        print(f"  [AVISO] {rushing_path} não encontrado — rushing ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(rushing_path, dtype=str)
    to_int(df, ["season", "week", "yards", "attempts", "long_gain", "touchdowns"])
    if "fumbles" in df.columns:
        df["fumbles"] = pd.to_numeric(df["fumbles"], errors="coerce").fillna(0).astype(int)
    else:
        df["fumbles"] = 0
    return df[cols]


def build_receiving(receiving_path: Path) -> pd.DataFrame:
    """Tabela `receiving`: lê receiving_<ano>.csv (extract_player_game_logs.py,
    uma linha por semana real). `fumbles` vem do par FUM/LOST único da
    página de Logs, atribuído a receiving quando a linha tem bloco de
    recepção na mesma semana (ver extract_player_game_logs.py)."""
    cols = ["player_id_team", "season", "week", "receptions", "yards", "touchdowns",
            "long_gain", "fumbles"]
    if not receiving_path.exists():
        print(f"  [AVISO] {receiving_path} não encontrado — receiving ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(receiving_path, dtype=str)
    to_int(df, ["season", "week", "receptions", "yards", "long_gain", "touchdowns"])
    if "fumbles" in df.columns:
        df["fumbles"] = pd.to_numeric(df["fumbles"], errors="coerce").fillna(0).astype(int)
    else:
        df["fumbles"] = 0
    return df[cols]


def build_kick_return(stats_dir: Path, roster: pd.DataFrame, year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    """Tabela `kick_return`: lê kickoff_returns_<ano>.csv (extract_category_stats.py,
    acumulado da temporada) e carimba cada linha com a última semana real
    do time do jogador (ver attach_player_id_team / determine_team_weeks).
    A coluna de touchdowns é localizada pelo primeiro nome de origem
    reconhecido, pois o nome exato pode variar conforme o layout da
    categoria."""
    cols = ["player_id_team", "season", "week", "returns", "yards", "average", "touchdowns",
            "returns_20_yards_plus", "returns_40_yards_plus", "long_gain", "fair_catches",
            "fumbles"]
    path = stats_dir / f"kickoff_returns_{year}.csv"
    if not path.exists():
        print(f"  [AVISO] {path} não encontrado (categoria veio vazia nesta execução? "
              f"ver log de extract_category_stats.py) — kick_return ficará vazio",
              file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(path, dtype=str)
    df = attach_player_id_team(df, roster, year, team_weeks)
    rename_map = {
        "avg": "average", "ret": "returns", "yds": "yards",
        "20plus": "returns_20_yards_plus", "40plus": "returns_40_yards_plus",
        "lng": "long_gain", "fc": "fair_catches", "fum": "fumbles",
    }
    for candidate_td in ("kret_td", "ret_td", "td"):
        if candidate_td in df.columns:
            rename_map[candidate_td] = "touchdowns"
            break
    df = df.rename(columns=rename_map)
    to_num(df, ["average"])
    to_int(df, ["returns", "yards", "touchdowns", "returns_20_yards_plus",
                "returns_40_yards_plus", "long_gain", "fair_catches", "fumbles"])
    return df[[c for c in cols if c in df.columns]]


def build_punt_return(stats_dir: Path, roster: pd.DataFrame, year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    """Tabela `punt_return`: lê punt_returns_<ano>.csv (extract_category_stats.py,
    acumulado da temporada) e carimba cada linha com a última semana real
    do time do jogador (ver attach_player_id_team / determine_team_weeks).
    Mesmo esquema de mapeamento de colunas de build_kick_return: a coluna
    de touchdowns é localizada pelo primeiro nome de origem reconhecido."""
    cols = ["player_id_team", "season", "week", "returns", "yards", "average", "touchdowns",
            "returns_20_yards_plus", "returns_40_yards_plus", "long_gain", "fair_catches",
            "fumbles"]
    path = stats_dir / f"punt_returns_{year}.csv"
    if not path.exists():
        print(f"  [AVISO] {path} não encontrado (categoria veio vazia nesta execução? "
              f"ver log de extract_category_stats.py) — punt_return ficará vazio",
              file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(path, dtype=str)
    df = attach_player_id_team(df, roster, year, team_weeks)
    rename_map = {
        "avg": "average", "ret": "returns", "yds": "yards",
        "20plus": "returns_20_yards_plus", "40plus": "returns_40_yards_plus",
        "lng": "long_gain", "fc": "fair_catches", "fum": "fumbles",
    }
    for candidate_td in ("pret_td", "kret_td", "td"):
        if candidate_td in df.columns:
            rename_map[candidate_td] = "touchdowns"
            break
    df = df.rename(columns=rename_map)
    to_num(df, ["average"])
    to_int(df, ["returns", "yards", "touchdowns", "returns_20_yards_plus",
                "returns_40_yards_plus", "long_gain", "fair_catches", "fumbles"])
    return df[[c for c in cols if c in df.columns]]


def build_punting(punting_path: Path) -> pd.DataFrame:
    """Tabela `punting`: lê punting_<ano>.csv (extract_player_game_logs.py,
    uma linha por semana real). A página de Logs de um punter traz todas
    as colunas da tabela `punting`, sem exceção."""
    cols = ["player_id_team", "season", "week", "punts", "yards", "net_yards", "long_gain",
            "average", "net_average", "blocked", "out_of_bounds", "downed",
            "in_20_yards_line", "touchbacks", "fair_catches_against", "returns_against",
            "return_yards_against", "return_touchdowns_against"]
    if not punting_path.exists():
        print(f"  [AVISO] {punting_path} não encontrado — punting ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(punting_path, dtype=str)
    to_num(df, ["average", "net_average"])
    to_int(df, ["season", "week", "net_yards", "punts", "long_gain", "yards",
                "in_20_yards_line", "out_of_bounds", "downed", "touchbacks",
                "fair_catches_against", "returns_against", "return_yards_against",
                "return_touchdowns_against", "blocked"])
    return df[cols]


def build_kicking(kicking_fg_ko_path: Path, extra_points_path: Path) -> pd.DataFrame:
    """Tabela `kicking`: junta field goals + kickoffs (kicking_fg_ko_<ano>.csv)
    com extra points (extra_points_<ano>.csv), ambos vindos de
    extract_player_game_logs.py com uma linha por semana real, pela chave
    (player_id_team, season, week)."""
    cols = ["player_id_team", "season", "week", "field_goals_made", "field_goal_attempts",
            "field_goal_pct", "field_goal_long", "field_goals_blocked", "kickoffs",
            "touchbacks", "kickoff_returns_against", "kickoff_return_avg_against",
            "extra_point_attempts", "extra_points_made", "extra_point_pct",
            "extra_points_blocked"]

    if kicking_fg_ko_path.exists():
        merged = pd.read_csv(kicking_fg_ko_path, dtype=str)
        to_num(merged, ["field_goal_pct", "kickoff_return_avg_against"])
        to_int(merged, ["season", "week", "field_goals_made", "field_goal_attempts",
                        "field_goal_long", "field_goals_blocked", "kickoffs", "touchbacks",
                        "kickoff_returns_against"])
    else:
        print(f"  [AVISO] {kicking_fg_ko_path} não encontrado — colunas de FG/KO ficarão zeradas",
              file=sys.stderr)
        merged = pd.DataFrame(columns=["player_id_team", "season", "week"])

    if extra_points_path.exists():
        xp = pd.read_csv(extra_points_path, dtype=str)
        to_num(xp, ["extra_point_pct"])
        to_int(xp, ["season", "week", "extra_point_attempts", "extra_points_made",
                    "extra_points_blocked"])
        merged = merged.merge(xp, on=["player_id_team", "season", "week"], how="outer")
    else:
        print(f"  [AVISO] {extra_points_path} não encontrado — colunas de "
              f"extra point ficarão nulas", file=sys.stderr)
        for c in ["extra_point_attempts", "extra_points_made", "extra_point_pct",
                  "extra_points_blocked"]:
            merged[c] = 0

    # o merge "outer" gera NaN nas colunas de um lado quando um kicker só
    # aparece em uma das duas fontes numa dada semana — corrige os tipos de
    # novo depois do merge, não só antes.
    kicking_int_cols = [
        "field_goals_made", "field_goal_attempts", "field_goal_long", "field_goals_blocked",
        "kickoffs", "touchbacks", "kickoff_returns_against",
        "extra_point_attempts", "extra_points_made", "extra_points_blocked",
    ]
    kicking_float_cols = ["field_goal_pct", "kickoff_return_avg_against", "extra_point_pct"]
    for c in kicking_int_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0).astype(int)
    for c in kicking_float_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0)
        else:
            merged[c] = 0.0

    for c in cols:
        if c not in merged.columns:
            merged[c] = 0
    return merged[cols]


def build_defense(defense_path: Path, stats_dir: Path, roster: pd.DataFrame,
                   year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    """Tabela `defense`: tackles/sacks/safeties/pass_defended vêm de
    defense_<ano>.csv (extract_player_game_logs.py, uma linha por semana
    real); interceptions vem de interceptions_<ano>.csv
    (extract_category_stats.py, acumulado da temporada, carimbado na
    última semana real do time via team_weeks). As duas fontes são unidas
    pela chave (player_id_team, season, week)."""
    if defense_path.exists():
        defense = pd.read_csv(defense_path, dtype=str)
        defense["season"] = pd.to_numeric(defense["season"], errors="coerce").astype("Int64")
        to_int(defense, ["combined_tackles", "solo_tackles", "assisted_tackles",
                          "safeties", "pass_defended", "week"])
        to_num(defense, ["sacks"])
    else:
        print(f"  [AVISO] {defense_path} não encontrado — colunas de tackle ficarão nulas",
              file=sys.stderr)
        defense = pd.DataFrame(columns=["player_id_team", "season", "week"])

    intc_path = stats_dir / f"interceptions_{year}.csv"
    intc_cols = ["player_id_team", "season", "week", "interceptions", "interception_touchdowns",
                 "interception_yards", "interception_long"]
    if intc_path.exists():
        intc = pd.read_csv(intc_path, dtype=str)
        intc = attach_player_id_team(intc, roster, year, team_weeks)
        intc = intc.rename(columns={
            "int": "interceptions", "int_td": "interception_touchdowns",
            "int_yds": "interception_yards", "lng": "interception_long",
        })
        to_int(intc, ["interceptions", "interception_touchdowns", "interception_yards",
                      "interception_long"])
        intc = intc[intc_cols]
    else:
        print(f"  [AVISO] {intc_path} não encontrado (categoria veio vazia nesta execução? "
              f"ver log de extract_category_stats.py) — colunas de interception ficarão zeradas",
              file=sys.stderr)
        intc = pd.DataFrame(columns=intc_cols)

    merged = defense.merge(intc, on=["player_id_team", "season", "week"], how="outer")

    # o merge "outer" introduz NaN nas colunas de um lado quando o jogador só
    # existe no outro (ex: tem tackle mas nunca interceptou) — precisa
    # limpar os tipos de novo DEPOIS do merge, não só antes.
    int_cols = ["combined_tackles", "solo_tackles", "assisted_tackles", "safeties",
                "pass_defended", "interceptions", "interception_touchdowns",
                "interception_yards", "interception_long"]
    for c in int_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0).astype(int)
    if "sacks" in merged.columns:
        merged["sacks"] = pd.to_numeric(merged["sacks"], errors="coerce").fillna(0)

    return merged


def build_fumbles(stats_dir: Path, rushing: pd.DataFrame, receiving: pd.DataFrame,
                   roster: pd.DataFrame, year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    """Tabela `fumbles`: forced_fumbles/opponent_fumbles_recovered/
    opponent_fumble_recovery_touchdowns vêm de fumbles_<ano>.csv
    (extract_category_stats.py, acumulado da temporada, carimbado na
    última semana real do time via team_weeks); own_fumbles é a soma dos
    fumbles já computados em rushing e receiving na mesma semana."""
    fumbles_path = stats_dir / f"fumbles_{year}.csv"
    df_cols = ["player_id_team", "season", "week", "forced_fumbles", "opponent_fumbles_recovered",
               "opponent_fumble_recovery_touchdowns"]
    if fumbles_path.exists():
        df = pd.read_csv(fumbles_path, dtype=str)
        df = attach_player_id_team(df, roster, year, team_weeks)
        df = df.rename(columns={
            "ff": "forced_fumbles", "fr": "opponent_fumbles_recovered",
            "fr_td": "opponent_fumble_recovery_touchdowns",
        })
        to_num(df, ["forced_fumbles", "opponent_fumbles_recovered",
                    "opponent_fumble_recovery_touchdowns"])
        df = df[df_cols]
    else:
        print(f"  [AVISO] {fumbles_path} não encontrado (categoria veio vazia nesta execução? "
              f"ver log de extract_category_stats.py) — forced_fumbles/opponent_* ficarão zerados",
              file=sys.stderr)
        df = pd.DataFrame(columns=df_cols)

    own = rushing[["player_id_team", "season", "week", "fumbles"]].rename(columns={"fumbles": "rush_fum"})
    own = own.merge(
        receiving[["player_id_team", "season", "week", "fumbles"]].rename(columns={"fumbles": "rec_fum"}),
        on=["player_id_team", "season", "week"], how="outer",
    )
    own["rush_fum"] = own["rush_fum"].fillna(0)
    own["rec_fum"] = own["rec_fum"].fillna(0)
    own["own_fumbles"] = own["rush_fum"] + own["rec_fum"]
    own = own[["player_id_team", "season", "week", "own_fumbles"]]

    merged = df.merge(own, on=["player_id_team", "season", "week"], how="outer")
    for c in ["forced_fumbles", "opponent_fumbles_recovered",
              "opponent_fumble_recovery_touchdowns", "own_fumbles"]:
        merged[c] = merged[c].fillna(0).astype(int)
    return merged


def build_downs(stats_dir: Path, year: int) -> pd.DataFrame:
    """Tabela `downs`: lê downs_<ano>.csv (extract_category_stats.py, uma
    linha por time, temporada inteira, sem coluna de semana)."""
    path = stats_dir / f"downs_{year}.csv"
    if not path.exists():
        print(f"  [AVISO] {path} não encontrado — downs não será carregado", file=sys.stderr)
        return pd.DataFrame(columns=["team_id", "season"])

    df = pd.read_csv(path, dtype=str)
    df["season"] = year
    int_cols = ["third_down_att", "third_down_made", "fourth_down_att", "fourth_down_made",
                "receiving_first_downs", "rushing_first_downs", "scrimmage_plays"]
    float_cols = ["receiving_first_down_pct", "rushing_first_down_pct"]
    to_int(df, int_cols)
    to_num(df, float_cols)
    cols = ["team_id", "season"] + int_cols + float_cols
    return df[cols]


def build_games(games_path: Path) -> pd.DataFrame:
    """Tabela `games`: lê games_<ano>.csv (extract_player_game_logs.py) e
    resolve o apelido do adversário (coluna `opponent`) para o team_id
    correspondente via NICKNAME_TO_TEAM_ID."""
    df = pd.read_csv(games_path, dtype=str)
    df["opponent_id"] = df["opponent"].map(NICKNAME_TO_TEAM_ID)

    unresolved = df[df["opponent_id"].isna()]["opponent"].unique()
    if len(unresolved):
        print(f"  [AVISO] apelidos de time não reconhecidos: {list(unresolved)} — "
              f"confira TEAM_NICKNAMES", file=sys.stderr)

    return df[["team_id", "season", "week", "opponent_id", "home_away", "win_loss",
               "made_points", "suffered_points"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--roster", required=True)
    parser.add_argument("--stats-dir", required=True,
                         help="saída de extract_category_stats.py (fumbles/interceptions/"
                              "kickoff-returns/punt-returns/downs)")
    parser.add_argument("--logs-dir", required=True,
                         help="saída de extract_player_game_logs.py (games/extra_points/"
                              "defense/passing/rushing/receiving/kicking_fg_ko/punting)")
    parser.add_argument("--output-dir", default="./final_output")
    args = parser.parse_args()

    stats_dir = Path(args.stats_dir)
    logs_dir = Path(args.logs_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    games_path = logs_dir / f"games_{args.year}.csv"
    extra_points_path = logs_dir / f"extra_points_{args.year}.csv"
    defense_logs_path = logs_dir / f"defense_{args.year}.csv"
    passing_path = logs_dir / f"passing_{args.year}.csv"
    rushing_path = logs_dir / f"rushing_{args.year}.csv"
    receiving_path = logs_dir / f"receiving_{args.year}.csv"
    kicking_fg_ko_path = logs_dir / f"kicking_fg_ko_{args.year}.csv"
    punting_path = logs_dir / f"punting_{args.year}.csv"

    if not games_path.exists():
        print(
            f"[ERRO] {games_path} não existe. Este script depende do CSV de jogos "
            "gerado por extract_player_game_logs.py (etapa anterior do pipeline) para "
            "saber a semana de cada time — sem ele não há como continuar. Confira o log "
            "dessa etapa: se ela reportou \"Zero jogos coletados\", o problema está lá "
            "(rede, layout do nfl.com mudou, ou roster vazio), não neste script.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Construindo teams (conf_div)...")
    build_teams().to_csv(out_dir / "teams_final.csv", index=False)

    print("Carregando roster...")
    roster = load_roster(Path(args.roster))

    # team_weeks é necessário pras categorias que continuam cumulativas
    # (fumbles/interceptions/kickoff-returns/punt-returns) — ver docstring
    # do módulo.
    team_weeks = determine_team_weeks(games_path)
    weeks_found = sorted(set(team_weeks.values()))
    if len(weeks_found) > 1:
        behind = sorted(t for t, w in team_weeks.items() if w < weeks_found[-1])
        print(f"[AVISO] Execução parcial: {len(behind)} time(s) ainda na semana "
              f"{weeks_found[0]} enquanto outros já estão na semana {weeks_found[-1]}: "
              f"{behind}. Cada jogador será carimbado com a semana real do PRÓPRIO "
              f"time (ver determine_team_weeks) — não com a maior semana da liga.",
              file=sys.stderr)
    else:
        print(f"Semana atual detectada (todos os times): {weeks_found[0]}")

    print("Construindo players...")
    build_players(roster, args.year).to_csv(out_dir / "players_final.csv", index=False)

    print("Construindo passing (fonte: Logs, semana real)...")
    build_passing(passing_path).to_csv(out_dir / "passing_final.csv", index=False)

    print("Construindo rushing (fonte: Logs, semana real)...")
    rushing = build_rushing(rushing_path)
    rushing.to_csv(out_dir / "rushing_final.csv", index=False)

    print("Construindo receiving (fonte: Logs, semana real)...")
    receiving = build_receiving(receiving_path)
    receiving.to_csv(out_dir / "receiving_final.csv", index=False)

    print("Construindo kick_return (fonte: categoria, cumulativo)...")
    build_kick_return(stats_dir, roster, args.year, team_weeks).to_csv(
        out_dir / "kick_return_final.csv", index=False)

    print("Construindo punt_return (fonte: categoria, cumulativo)...")
    build_punt_return(stats_dir, roster, args.year, team_weeks).to_csv(
        out_dir / "punt_return_final.csv", index=False)

    print("Construindo punting (fonte: Logs, semana real)...")
    build_punting(punting_path).to_csv(out_dir / "punting_final.csv", index=False)

    print("Construindo kicking (FG+KO+XP, fonte: Logs, semana real)...")
    build_kicking(kicking_fg_ko_path, extra_points_path).to_csv(
        out_dir / "kicking_final.csv", index=False)

    print("Construindo defense (tackles: Logs semana real / interceptions: categoria)...")
    build_defense(defense_logs_path, stats_dir, roster, args.year, team_weeks).to_csv(
        out_dir / "defense_final.csv", index=False)

    print("Construindo fumbles (defensivo: categoria / próprio: rushing+receiving)...")
    build_fumbles(stats_dir, rushing, receiving, roster, args.year, team_weeks).to_csv(
        out_dir / "fumbles_final.csv", index=False)

    print("Construindo games...")
    build_games(games_path).to_csv(out_dir / "games_final.csv", index=False)

    print("Construindo downs...")
    build_downs(stats_dir, args.year).to_csv(out_dir / "downs_final.csv", index=False)

    print(f"\nConcluído. CSVs finais em: {out_dir}/")


if __name__ == "__main__":
    main()