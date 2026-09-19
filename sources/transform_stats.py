"""
transform_stats.py

Fase 3 do pipeline: junta todos os CSVs gerados pelos scripts de extração
(roster, categorias de líderes, games, extra_points, defense) e produz um
CSV final por tabela do banco, já com as colunas renomeadas para bater
com o DDL e prontos para o script de upsert no Supabase.

Entradas esperadas (ajuste os caminhos via argumentos se necessário):
    players_roster.csv                 (extract_players_roster.py)
    stats_dir/passing_<ano>.csv         (extract_category_stats.py)
    stats_dir/rushing_<ano>.csv
    stats_dir/receiving_<ano>.csv
    stats_dir/fumbles_<ano>.csv           (lado defensivo: FF/FR/FR TD)
    stats_dir/interceptions_<ano>.csv
    stats_dir/field_goals_<ano>.csv
    stats_dir/kickoffs_<ano>.csv
    stats_dir/kickoff_returns_<ano>.csv
    stats_dir/punts_<ano>.csv
    stats_dir/punt_returns_<ano>.csv
    stats_dir/downs_<ano>.csv            (extract_category_stats.py — tabela por time)
    games_<ano>.csv                      (extract_qb_games.py)
    extra_points_<ano>.csv               (extract_extra_points.py)
    defense_<ano>.csv                    (extract_defense_stats.py — tackles)

Saídas (em --output-dir): teams_final.csv, players_final.csv, passing_final.csv,
rushing_final.csv, receiving_final.csv, kicking_final.csv,
kick_return_final.csv, punt_return_final.csv, punting_final.csv,
defense_final.csv, fumbles_final.csv, games_final.csv, downs_final.csv

teams_final.csv: NÃO depende de nenhuma extração (conference/division são
estáticas), só existe pra alimentar a coluna `conf_div` da tabela `teams`
(ex: "NFC East") via upsert em load_to_supabase.py — as demais colunas de
`teams` continuam vindo do seed original, fora deste pipeline.

GAP CONHECIDO / LIMITAÇÃO DA FONTE: as categorias de líderes não separam
estatísticas por time — um jogador negociado no meio da temporada
aparece com o total acumulado da temporada inteira sob o TIME ATUAL
(mesmo se uma parte das estatísticas foi feita pelo time anterior). Isso
é uma limitação do próprio nfl.com nessas páginas, não deste script: o
`player_id_team` é montado com o time do roster mais recente.

Uso:
    python transform_stats.py --year 2026 \
        --roster players_roster.csv \
        --stats-dir ./stats_2026 \
        --games games_2026.csv \
        --extra-points extra_points_2026.csv \
        --defense defense_2026.csv \
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


def determine_current_week(games_path: Path) -> int:
    """A extração de jogos (extract_qb_games.py) já nos diz, por si só,
    qual é a semana mais recente já disputada: o maior valor de 'week'
    encontrado no CSV de jogos gerado nesta mesma rodada. Usamos isso
    como o número da semana do snapshot desta execução."""
    df = pd.read_csv(games_path, dtype=str)
    week = pd.to_numeric(df["week"], errors="coerce").max()
    if pd.isna(week):
        raise ValueError(f"Não foi possível determinar a semana atual a partir de {games_path}")
    return int(week)


def attach_player_id_team(df: pd.DataFrame, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    """Junta um CSV de categoria (só tem player_id) com o roster para
    obter team_id e montar player_id_team."""
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
    merged["week"] = week
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
    """Tabela `teams`, só a coluna nova `conf_div` (ex: "NFC East").
    Não depende de nenhum CSV de extração — vem de TEAM_CONF_DIV, um mapa
    estático (a divisão de um time só muda em realinhamentos raros da
    liga). As demais colunas de `teams` (nome, cidade, logo, etc.)
    continuam vindo do seed original e não são tocadas por este pipeline;
    o upsert em load_to_supabase.py atualiza só `conf_div` para cada
    team_id já existente."""
    rows = [{"team_id": team_id, "conf_div": conf_div} for team_id, conf_div in TEAM_CONF_DIV.items()]
    return pd.DataFrame(rows, columns=["team_id", "conf_div"])


def build_players(roster: pd.DataFrame, year: int) -> pd.DataFrame:
    out = roster.rename(columns={"position": "player_position"})[
        ["player_id_team", "player_id", "player_name", "player_position", "team_id"]
    ].copy()
    out["season"] = year
    out["player_image_url"] = None  # não capturado pelo roster scraper ainda
    return out[["player_id_team", "season", "player_id", "player_name", "player_position",
                "team_id", "player_image_url"]]


def build_passing(stats_dir: Path, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"passing_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    df = df.rename(columns={
        "pass_yds": "yards", "yds_per_att": "yards_per_attempt", "att": "attempts",
        "cmp": "completions", "cmp_pct": "completion_pct", "td": "touchdowns",
        "int": "interceptions", "rate": "rate", "1st": "first_downs",
        "1stpct": "first_down_pct", "20plus": "pass_20_yards_plus",
        "40plus": "pass_40_yards_plus", "lng": "long_gain", "sck": "sacks",
        "scky": "sacks_yards",
    })
    to_num(df, ["yards_per_attempt", "completion_pct", "rate", "first_down_pct"])
    to_int(df, ["yards", "attempts", "completions", "touchdowns", "interceptions",
                "first_downs", "pass_20_yards_plus", "pass_40_yards_plus", "long_gain",
                "sacks", "sacks_yards"])
    cols = ["player_id_team", "season", "week", "yards", "yards_per_attempt", "attempts", "completions",
            "completion_pct", "touchdowns", "interceptions", "rate", "first_downs",
            "first_down_pct", "pass_20_yards_plus", "pass_40_yards_plus", "long_gain",
            "sacks", "sacks_yards"]
    return df[cols]


def build_rushing(stats_dir: Path, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"rushing_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    df = df.rename(columns={
        "rush_yds": "yards", "att": "attempts", "td": "touchdowns",
        "20plus": "rush_20_yards_plus", "40plus": "rush_40_yards_plus",
        "lng": "long_gain", "rush_1st": "first_downs", "rush_1stpct": "first_down_pct",
        "rush_fum": "fumbles",
    })
    to_num(df, ["first_down_pct"])
    to_int(df, ["yards", "attempts", "touchdowns", "rush_20_yards_plus",
                "rush_40_yards_plus", "long_gain", "first_downs", "fumbles"])
    cols = ["player_id_team", "season", "week", "yards", "attempts", "touchdowns",
            "rush_20_yards_plus", "rush_40_yards_plus", "long_gain", "first_downs",
            "first_down_pct", "fumbles"]
    return df[cols]


def build_receiving(stats_dir: Path, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"receiving_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    df = df.rename(columns={
        "rec": "receptions", "yds": "yards", "td": "touchdowns",
        "20plus": "reception_20_yards_plus", "40plus": "receptions_40_yards_plus",
        "lng": "long_gain", "rec_1st": "first_downs", "1stpct": "first_down_pct",
        "rec_fum": "fumbles", "rec_yac_per_r": "yards_after_catch", "tgts": "targets",
    })
    to_num(df, ["first_down_pct", "yards_after_catch"])
    to_int(df, ["receptions", "yards", "touchdowns", "reception_20_yards_plus",
                "receptions_40_yards_plus", "long_gain", "first_downs", "fumbles", "targets"])
    cols = ["player_id_team", "season", "week", "receptions", "yards", "touchdowns",
            "reception_20_yards_plus", "receptions_40_yards_plus", "long_gain",
            "first_downs", "first_down_pct", "fumbles", "yards_after_catch", "targets"]
    return df[cols]


def build_kick_return(stats_dir: Path, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"kickoff_returns_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    df = df.rename(columns={
        "avg": "average", "ret": "returns", "yds": "yards", "kret_td": "touchdowns",
        "20plus": "returns_20_yards_plus", "40plus": "returns_40_yards_plus",
        "lng": "long_gain", "fc": "fair_catches", "fum": "fumbles",
    })
    to_num(df, ["average"])
    to_int(df, ["returns", "yards", "touchdowns", "returns_20_yards_plus",
                "returns_40_yards_plus", "long_gain", "fair_catches", "fumbles"])
    cols = ["player_id_team", "season", "week", "returns", "yards", "average", "touchdowns",
            "returns_20_yards_plus", "returns_40_yards_plus", "long_gain", "fair_catches",
            "fumbles"]
    return df[cols]


def build_punt_return(stats_dir: Path, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"punt_returns_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    # ATENÇÃO: mapeamento assumido igual ao de Kickoff Returns — ainda não
    # conferido coluna a coluna contra a extração real. Ajustar se os nomes
    # de coluna vierem diferentes (ex: "PRet TD" em vez de "KRet TD").
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
    cols = ["player_id_team", "season", "week", "returns", "yards", "average", "touchdowns",
            "returns_20_yards_plus", "returns_40_yards_plus", "long_gain", "fair_catches",
            "fumbles"]
    return df[[c for c in cols if c in df.columns]]


def build_punting(stats_dir: Path, roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"punts_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    df = df.rename(columns={
        "avg": "average", "net_avg": "net_average", "net_yds": "net_yards",
        "punts": "punts", "lng": "long_gain", "yds": "yards", "in_20": "in_20_yards_line",
        "oob": "out_of_bounds", "dn": "downed", "tb": "touchbacks",
        "fc": "fair_catches_against", "ret": "returns_against", "rety": "return_yards_against",
        "td": "return_touchdowns_against", "p_blk": "blocked",
    })
    to_num(df, ["average", "net_average"])
    to_int(df, ["net_yards", "punts", "long_gain", "yards",
                "in_20_yards_line", "out_of_bounds", "downed", "touchbacks",
                "fair_catches_against", "returns_against", "return_yards_against",
                "return_touchdowns_against", "blocked"])
    cols = ["player_id_team", "season", "week", "punts", "yards", "net_yards", "long_gain",
            "average", "net_average", "blocked", "out_of_bounds", "downed",
            "in_20_yards_line", "touchbacks", "fair_catches_against", "returns_against",
            "return_yards_against", "return_touchdowns_against"]
    return df[cols]


def build_kicking(stats_dir: Path, roster: pd.DataFrame, year: int, week: int,
                   extra_points_path: Path) -> pd.DataFrame:
    fg = pd.read_csv(stats_dir / f"field_goals_{year}.csv", dtype=str)
    fg = attach_player_id_team(fg, roster, year, week)
    fg = fg.rename(columns={
        "fgm": "field_goals_made", "att": "field_goal_attempts", "fg_pct": "field_goal_pct",
        "lng": "field_goal_long", "fg_blk": "field_goals_blocked",
    })
    fg = split_att_made(fg, "1_19_a_m", "fg_made_1_19", "fg_att_1_19")
    fg = split_att_made(fg, "20_29_a_m", "fg_made_20_29", "fg_att_20_29")
    fg = split_att_made(fg, "30_39_a_m", "fg_made_30_39", "fg_att_30_39")
    fg = split_att_made(fg, "40_49_a_m", "fg_made_40_49", "fg_att_40_49")
    fg = split_att_made(fg, "50_59_a_m", "fg_made_50_59", "fg_att_50_59")
    fg = split_att_made(fg, "60plus_a_m", "fg_made_60_plus", "fg_att_60_plus")
    to_num(fg, ["field_goal_pct"])
    to_int(fg, ["field_goals_made", "field_goal_attempts", "field_goal_long",
                "field_goals_blocked"])

    ko = pd.read_csv(stats_dir / f"kickoffs_{year}.csv", dtype=str)
    ko = attach_player_id_team(ko, roster, year, week)
    ko = ko.rename(columns={
        "ko": "kickoffs", "yds": "kickoff_yards", "ret_yds": "kickoff_return_yards_against",
        "tb": "touchbacks", "tb_pct": "touchback_pct", "ret": "kickoff_returns_against",
        "ret_avg": "kickoff_return_avg_against", "osk": "onside_kicks",
        "osk_rec": "onside_kicks_recovered", "oob": "kickoffs_out_of_bounds",
        "td": "kickoff_return_touchdowns_against",
    })
    to_num(ko, ["touchback_pct", "kickoff_return_avg_against"])
    to_int(ko, ["kickoffs", "kickoff_yards", "kickoff_return_yards_against", "touchbacks",
                "kickoff_returns_against", "onside_kicks", "onside_kicks_recovered",
                "kickoffs_out_of_bounds", "kickoff_return_touchdowns_against"])

    fg_cols = ["player_id_team", "season", "week", "field_goals_made", "field_goal_attempts",
               "field_goal_pct", "fg_made_1_19", "fg_att_1_19", "fg_made_20_29",
               "fg_att_20_29", "fg_made_30_39", "fg_att_30_39", "fg_made_40_49",
               "fg_att_40_49", "fg_made_50_59", "fg_att_50_59", "fg_made_60_plus",
               "fg_att_60_plus", "field_goal_long", "field_goals_blocked"]
    ko_cols = ["player_id_team", "season", "week", "kickoffs", "kickoff_yards",
               "kickoff_return_yards_against", "touchbacks", "touchback_pct",
               "kickoff_returns_against", "kickoff_return_avg_against", "onside_kicks",
               "onside_kicks_recovered", "kickoffs_out_of_bounds",
               "kickoff_return_touchdowns_against"]

    merged = fg[fg_cols].merge(ko[ko_cols], on=["player_id_team", "season", "week"], how="outer")

    if extra_points_path.exists():
        xp = pd.read_csv(extra_points_path, dtype=str)
        xp["season"] = pd.to_numeric(xp["season"], errors="coerce").astype("Int64")
        xp["week"] = week
        to_num(xp, ["extra_point_pct"])
        to_int(xp, ["extra_point_attempts", "extra_points_made", "extra_points_blocked"])
        merged = merged.merge(xp, on=["player_id_team", "season", "week"], how="left")
    else:
        print(f"  [AVISO] {extra_points_path} não encontrado — colunas de "
              f"extra point ficarão nulas", file=sys.stderr)
        for c in ["extra_point_attempts", "extra_points_made", "extra_point_pct",
                  "extra_points_blocked"]:
            merged[c] = 0

    # os merges "outer"/"left" acima introduzem NaN quando um jogador só
    # aparece em uma das 3 fontes (ex: kicker sem kickoff registrado ainda),
    # o que reverte colunas inteiras pra float64 de novo — corrige depois de
    # todos os merges, não só antes.
    kicking_int_cols = [
        "field_goals_made", "field_goal_attempts", "fg_made_1_19", "fg_att_1_19",
        "fg_made_20_29", "fg_att_20_29", "fg_made_30_39", "fg_att_30_39",
        "fg_made_40_49", "fg_att_40_49", "fg_made_50_59", "fg_att_50_59",
        "fg_made_60_plus", "fg_att_60_plus", "field_goal_long", "field_goals_blocked",
        "kickoffs", "kickoff_yards", "kickoff_return_yards_against", "touchbacks",
        "kickoff_returns_against", "onside_kicks", "onside_kicks_recovered",
        "kickoffs_out_of_bounds", "kickoff_return_touchdowns_against",
        "extra_point_attempts", "extra_points_made", "extra_points_blocked",
    ]
    kicking_float_cols = ["field_goal_pct", "touchback_pct",
                          "kickoff_return_avg_against", "extra_point_pct"]
    for c in kicking_int_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0).astype(int)
    for c in kicking_float_cols:
        if c in merged.columns:
            merged[c] = pd.to_numeric(merged[c], errors="coerce").fillna(0)

    return merged


def build_defense(defense_path: Path, stats_dir: Path, roster: pd.DataFrame,
                   year: int, week: int) -> pd.DataFrame:
    if defense_path.exists():
        defense = pd.read_csv(defense_path, dtype=str)
        defense["season"] = pd.to_numeric(defense["season"], errors="coerce").astype("Int64")
        defense["week"] = week
        to_num(defense, ["sacks"])
        to_int(defense, ["combined_tackles", "solo_tackles", "assisted_tackles",
                          "safeties", "pass_defended"])
    else:
        print(f"  [AVISO] {defense_path} não encontrado — colunas de tackle ficarão nulas",
              file=sys.stderr)
        defense = pd.DataFrame(columns=["player_id_team", "season", "week"])

    intc = pd.read_csv(stats_dir / f"interceptions_{year}.csv", dtype=str)
    intc = attach_player_id_team(intc, roster, year, week)
    intc = intc.rename(columns={
        "int": "interceptions", "int_td": "interception_touchdowns",
        "int_yds": "interception_yards", "lng": "interception_long",
    })
    to_int(intc, ["interceptions", "interception_touchdowns", "interception_yards",
                  "interception_long"])
    intc_cols = ["player_id_team", "season", "week", "interceptions", "interception_touchdowns",
                 "interception_yards", "interception_long"]

    merged = defense.merge(intc[intc_cols], on=["player_id_team", "season", "week"], how="outer")

    # o merge "outer" introduz NaN nas colunas de um lado quando o jogador só
    # existe no outro (ex: tem tackle mas nunca interceptou) — isso faz o
    # pandas reverter a coluna pra float64 de novo. Precisa limpar de novo
    # DEPOIS do merge, não só antes.
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
                   roster: pd.DataFrame, year: int, week: int) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"fumbles_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, week)
    df = df.rename(columns={
        "ff": "forced_fumbles", "fr": "opponent_fumbles_recovered",
        "fr_td": "opponent_fumble_recovery_touchdowns",
    })
    to_num(df, ["forced_fumbles", "opponent_fumbles_recovered",
                "opponent_fumble_recovery_touchdowns"])
    df = df[["player_id_team", "season", "week", "forced_fumbles", "opponent_fumbles_recovered",
             "opponent_fumble_recovery_touchdowns"]]

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
    parser.add_argument("--stats-dir", required=True)
    parser.add_argument("--games", required=True)
    parser.add_argument("--extra-points", required=True)
    parser.add_argument("--defense", required=True)
    parser.add_argument("--output-dir", default="./final_output")
    args = parser.parse_args()

    stats_dir = Path(args.stats_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Construindo teams (conf_div)...")
    build_teams().to_csv(out_dir / "teams_final.csv", index=False)

    print("Carregando roster...")
    roster = load_roster(Path(args.roster))

    week = determine_current_week(Path(args.games))
    print(f"Semana atual detectada: {week} (maior 'week' encontrado em {args.games})")

    print("Construindo players...")
    build_players(roster, args.year).to_csv(out_dir / "players_final.csv", index=False)

    print("Construindo passing...")
    passing = build_passing(stats_dir, roster, args.year, week)
    passing.to_csv(out_dir / "passing_final.csv", index=False)

    print("Construindo rushing...")
    rushing = build_rushing(stats_dir, roster, args.year, week)
    rushing.to_csv(out_dir / "rushing_final.csv", index=False)

    print("Construindo receiving...")
    receiving = build_receiving(stats_dir, roster, args.year, week)
    receiving.to_csv(out_dir / "receiving_final.csv", index=False)

    print("Construindo kick_return...")
    build_kick_return(stats_dir, roster, args.year, week).to_csv(
        out_dir / "kick_return_final.csv", index=False)

    print("Construindo punt_return...")
    build_punt_return(stats_dir, roster, args.year, week).to_csv(
        out_dir / "punt_return_final.csv", index=False)

    print("Construindo punting...")
    build_punting(stats_dir, roster, args.year, week).to_csv(
        out_dir / "punting_final.csv", index=False)

    print("Construindo kicking (Field Goals + Kickoffs + Extra Points)...")
    build_kicking(stats_dir, roster, args.year, week, Path(args.extra_points)).to_csv(
        out_dir / "kicking_final.csv", index=False)

    print("Construindo defense (tackles + interceptions)...")
    build_defense(Path(args.defense), stats_dir, roster, args.year, week).to_csv(
        out_dir / "defense_final.csv", index=False)

    print("Construindo fumbles (defensivo + próprio)...")
    build_fumbles(stats_dir, rushing, receiving, roster, args.year, week).to_csv(
        out_dir / "fumbles_final.csv", index=False)

    print("Construindo games...")
    build_games(Path(args.games)).to_csv(out_dir / "games_final.csv", index=False)

    print("Construindo downs...")
    build_downs(stats_dir, args.year).to_csv(out_dir / "downs_final.csv", index=False)

    print(f"\nConcluído. CSVs finais em: {out_dir}/")


if __name__ == "__main__":
    main()