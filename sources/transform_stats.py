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
    stats_dir/fumbles_<ano>.csv              (extract_category_stats.py — GAP: cumulativo, sem semana)
    stats_dir/interceptions_<ano>.csv        (idem)
    stats_dir/kickoff_returns_<ano>.csv      (idem)
    stats_dir/punt_returns_<ano>.csv         (idem)
    stats_dir/downs_<ano>.csv                (extract_category_stats.py — tabela por time, sem `week`, ok)

STATUS DA MIGRAÇÃO (ver seção 10 da documentação do projeto): games,
extra_points, defense (tackles), passing, rushing, receiving, kicking
(FG+KO+XP) e punting já vêm de extract_player_game_logs.py — semana real,
sobrevivem a uma reexecução do zero em qualquer momento da temporada.
Só fumbles, interceptions, kickoff_return e punt_return continuam presos
à página de líderes cumulativa (extract_category_stats.py) — kickoff/
punt return porque a página de Logs simplesmente não tem essas colunas
em lugar nenhum (confirmado contra um retornador titular antes de
decidir isso); fumbles/interceptions porque a página de Logs não separa
esses dados do jeito que a categoria separava (ver docstring de
extract_player_game_logs.py pro detalhe de cada limitação).

GAPS DE COLUNA que sobraram mesmo nas tabelas já migradas: a página de
Logs tem menos detalhamento que a página de líderes tinha. Ficam
zerados, sem outra fonte disponível: em passing, first_downs,
first_down_pct, pass_20/40_yards_plus e long_gain; em rushing/receiving,
first_downs, first_down_pct, os campos *_20/40_yards_plus, targets
(receiving) e yards_after_catch (receiving); em kicking, o
detalhamento de field goal por faixa de distância (fg_made_1_19 ...
fg_att_60_plus), kickoff_yards, kickoff_return_yards_against,
touchback_pct, onside_kicks, onside_kicks_recovered,
kickoffs_out_of_bounds e kickoff_return_touchdowns_against. punting é a
exceção: migrou sem perder nenhuma coluna.

Saídas (em --output-dir): teams_final.csv, players_final.csv, passing_final.csv,
rushing_final.csv, receiving_final.csv, kicking_final.csv,
kick_return_final.csv, punt_return_final.csv, punting_final.csv,
defense_final.csv, fumbles_final.csv, games_final.csv, downs_final.csv

teams_final.csv: NÃO depende de nenhuma extração (conference/division são
estáticas), só existe pra alimentar a coluna `conf_div` da tabela `teams`
(ex: "NFC East") via upsert em load_to_supabase.py — as demais colunas de
`teams` continuam vindo do seed original, fora deste pipeline.

GAP CONHECIDO / LIMITAÇÃO DA FONTE: tanto a página de líderes quanto a de
Logs não separam estatísticas por time — um jogador negociado no meio da
temporada aparece com o total (ou a semana) sob o TIME ATUAL, mesmo que
parte da produção tenha sido pelo time anterior. Isso é uma limitação do
próprio nfl.com, não deste script: o `player_id_team` é sempre montado
com o time do roster mais recente.

SEMANA POR TIME, NÃO GLOBAL (ainda relevante pras 4 categorias que
seguem cumulativas): como fumbles/interceptions/kickoff_return/
punt_return trazem o total ACUMULADO DA TEMPORADA por jogador, não um
valor por semana, o script precisa decidir sob qual "semana" gravar esse
total. Ele faz isso via determine_team_weeks(), que olha, TIME A TIME,
qual foi a última semana com jogo disputado no CSV de games desta mesma
rodada — não um único número de semana "global" (a maior de toda a
liga). Isso importa em execuções fora do dia de cron normal (ex: rodando
no meio da semana pra validar algo, enquanto só alguns times já
jogaram): um jogador de um time que ainda não jogou continua sendo
gravado sob a última semana REAL do próprio time (sobrescrevendo a mesma
linha, idempotente) até que o jogo dele apareça no log — em vez de criar
uma linha "semana N" espúria, idêntica à "semana N-1", só porque outro
time qualquer já jogou a rodada N.

LIMITAÇÃO QUE PERSISTE — reexecução do zero NÃO reconstrói o histórico
semana-a-semana de fumbles/interceptions/kickoff_return/punt_return:
essas 4 tabelas ainda leem stats_dir/<categoria>_<ano>.csv, cumulativo
sem histórico por semana (ver docstring de extract_category_stats.py).
A única forma de recuperar o histórico dessas 4 depois do fato é backup
do banco ou os CSVs finais (final_<ano>/*.csv) de execuções passadas.

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
    jogos desta rodada (extract_qb_games.py) — não um único número global.

    Por quê: as categorias de líderes (extract_category_stats.py) trazem o
    TOTAL ACUMULADO DA TEMPORADA de cada jogador, não um valor por semana.
    Se a execução acontece no meio da semana — nem todos os 32 times já
    jogaram — usar a MAIOR semana entre TODOS os times faz o script
    carimbar jogadores de times que ainda não jogaram com o número da
    semana seguinte, mesmo que o total deles ainda seja o da semana
    anterior (o jogador simplesmente ainda não jogou). Isso cria uma linha
    nova "semana N" idêntica à "semana N-1", uma duplicata falsa no banco.

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
    """Converte uma coluna tipo '8/8' (feito/tentado) em duas colunas numéricas.
    Não é mais usada por nenhum build_* desde que field-goals saiu das
    categorias de líderes (rodada 2) — a página de Logs não tem esse
    formato "feito/tentado" por faixa de distância. Mantida por se um dia
    outra categoria com esse mesmo formato precisar dela de novo."""
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


def build_passing(passing_path: Path) -> pd.DataFrame:
    """Migrado (rodada 2) para extract_player_game_logs.py — cada linha já
    é uma semana real, sem precisar de roster/team_weeks. A página de Logs
    não tem first_downs, first_down_pct, pass_20_yards_plus,
    pass_40_yards_plus nem long_gain de passing — ficam zerados (ver
    docstring do módulo e de extract_player_game_logs.py)."""
    cols = ["player_id_team", "season", "week", "yards", "yards_per_attempt", "attempts", "completions",
            "completion_pct", "touchdowns", "interceptions", "rate", "first_downs",
            "first_down_pct", "pass_20_yards_plus", "pass_40_yards_plus", "long_gain",
            "sacks", "sacks_yards"]
    if not passing_path.exists():
        print(f"  [AVISO] {passing_path} não encontrado — passing ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(passing_path, dtype=str)
    to_num(df, ["yards_per_attempt", "rate", "completion_pct"])
    to_int(df, ["season", "week", "yards", "attempts", "completions", "touchdowns",
                "interceptions", "sacks", "sacks_yards"])
    for c in ["first_downs", "pass_20_yards_plus", "pass_40_yards_plus", "long_gain"]:
        df[c] = 0  # não disponível na página de Logs (só no acumulado de categoria)
    df["first_down_pct"] = 0.0
    return df[cols]


def build_rushing(rushing_path: Path) -> pd.DataFrame:
    """Migrado (rodada 2). fumbles vem do FUM/LOST único da página de Logs
    (ver docstring de extract_player_game_logs.py: aproximação quando o
    jogador também tem bloco de recepção na mesma semana). first_downs,
    first_down_pct, rush_20_yards_plus e rush_40_yards_plus não existem na
    página de Logs — ficam zerados."""
    cols = ["player_id_team", "season", "week", "yards", "attempts", "touchdowns",
            "rush_20_yards_plus", "rush_40_yards_plus", "long_gain", "first_downs",
            "first_down_pct", "fumbles"]
    if not rushing_path.exists():
        print(f"  [AVISO] {rushing_path} não encontrado — rushing ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(rushing_path, dtype=str)
    to_int(df, ["season", "week", "yards", "attempts", "long_gain", "touchdowns"])
    if "fumbles" in df.columns:
        df["fumbles"] = pd.to_numeric(df["fumbles"], errors="coerce").fillna(0).astype(int)
    else:
        df["fumbles"] = 0
    for c in ["rush_20_yards_plus", "rush_40_yards_plus", "first_downs"]:
        df[c] = 0  # não disponível na página de Logs
    df["first_down_pct"] = 0.0
    return df[cols]


def build_receiving(receiving_path: Path) -> pd.DataFrame:
    """Migrado (rodada 2). Mesma aproximação de fumbles de build_rushing.
    first_downs, first_down_pct, reception_20/40_yards_plus, targets e
    yards_after_catch não existem na página de Logs (targets nunca
    apareceu em nenhuma amostra conferida, nem para RB nem WR) — ficam
    zerados."""
    cols = ["player_id_team", "season", "week", "receptions", "yards", "touchdowns",
            "reception_20_yards_plus", "receptions_40_yards_plus", "long_gain",
            "first_downs", "first_down_pct", "fumbles", "yards_after_catch", "targets"]
    if not receiving_path.exists():
        print(f"  [AVISO] {receiving_path} não encontrado — receiving ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(receiving_path, dtype=str)
    to_int(df, ["season", "week", "receptions", "yards", "long_gain", "touchdowns"])
    if "fumbles" in df.columns:
        df["fumbles"] = pd.to_numeric(df["fumbles"], errors="coerce").fillna(0).astype(int)
    else:
        df["fumbles"] = 0
    for c in ["reception_20_yards_plus", "receptions_40_yards_plus", "first_downs", "targets"]:
        df[c] = 0  # não disponível na página de Logs
    df["first_down_pct"] = 0.0
    df["yards_after_catch"] = 0.0
    return df[cols]


def build_kick_return(stats_dir: Path, roster: pd.DataFrame, year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    """NÃO migrado (ver seção 10 / docstring de extract_player_game_logs.py):
    a página de Logs não tem colunas de kick return em lugar nenhum,
    confirmado contra a página de um retornador titular. Continua
    cumulativo, carimbado via team_weeks."""
    df = pd.read_csv(stats_dir / f"kickoff_returns_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, team_weeks)
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


def build_punt_return(stats_dir: Path, roster: pd.DataFrame, year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    """NÃO migrado — mesmo motivo de build_kick_return."""
    df = pd.read_csv(stats_dir / f"punt_returns_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, team_weeks)
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


def build_punting(punting_path: Path) -> pd.DataFrame:
    """Migrado (rodada 2). Ao contrário de passing/rushing/receiving/
    kicking, a página de Logs de um punter tem TODAS as colunas que a
    categoria de líderes tinha — sem gap nenhum aqui."""
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
    """Migrado (rodada 2) para o FG/KO da página de Logs — junto com o XP
    (já migrado na rodada 1), TODO o kicking agora vem de semana real,
    sem precisar de roster/team_weeks nem inferência nenhuma.

    GAPS que continuam zerados porque a página de Logs não tem essas
    colunas em lugar nenhum (só a categoria de líderes tinha, e essa
    categoria saiu de extract_category_stats.py): o detalhamento de field
    goal por faixa de distância (fg_made_1_19 ... fg_att_60_plus),
    kickoff_yards (só a MÉDIA por kickoff está disponível, não o total —
    multiplicar de volta introduziria erro de arredondamento, por isso
    não é feito), kickoff_return_yards_against, touchback_pct,
    onside_kicks, onside_kicks_recovered, kickoffs_out_of_bounds e
    kickoff_return_touchdowns_against.
    """
    cols = ["player_id_team", "season", "week", "field_goals_made", "field_goal_attempts",
            "field_goal_pct", "fg_made_1_19", "fg_att_1_19", "fg_made_20_29",
            "fg_att_20_29", "fg_made_30_39", "fg_att_30_39", "fg_made_40_49",
            "fg_att_40_49", "fg_made_50_59", "fg_att_50_59", "fg_made_60_plus",
            "fg_att_60_plus", "field_goal_long", "field_goals_blocked",
            "kickoffs", "kickoff_yards", "kickoff_return_yards_against", "touchbacks",
            "touchback_pct", "kickoff_returns_against", "kickoff_return_avg_against",
            "onside_kicks", "onside_kicks_recovered", "kickoffs_out_of_bounds",
            "kickoff_return_touchdowns_against", "extra_point_attempts",
            "extra_points_made", "extra_point_pct", "extra_points_blocked"]

    if kicking_fg_ko_path.exists():
        merged = pd.read_csv(kicking_fg_ko_path, dtype=str)
        to_num(merged, ["field_goal_pct", "kickoff_avg", "kickoff_return_avg_against"])
        to_int(merged, ["season", "week", "field_goals_made", "field_goal_attempts",
                        "field_goal_long", "field_goals_blocked", "kickoffs", "touchbacks",
                        "kickoff_returns_against"])
        for c in ["fg_made_1_19", "fg_att_1_19", "fg_made_20_29", "fg_att_20_29",
                  "fg_made_30_39", "fg_att_30_39", "fg_made_40_49", "fg_att_40_49",
                  "fg_made_50_59", "fg_att_50_59", "fg_made_60_plus", "fg_att_60_plus",
                  "kickoff_yards", "kickoff_return_yards_against", "onside_kicks",
                  "onside_kicks_recovered", "kickoffs_out_of_bounds",
                  "kickoff_return_touchdowns_against"]:
            merged[c] = 0
        merged["touchback_pct"] = 0.0
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

    # o merge "outer" introduz NaN quando um kicker só aparece em uma das
    # duas fontes numa dada semana — corrige depois do merge, não só antes.
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
        else:
            merged[c] = 0.0

    for c in cols:
        if c not in merged.columns:
            merged[c] = 0
    return merged[cols]


def build_defense(defense_path: Path, stats_dir: Path, roster: pd.DataFrame,
                   year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    if defense_path.exists():
        defense = pd.read_csv(defense_path, dtype=str)
        defense["season"] = pd.to_numeric(defense["season"], errors="coerce").astype("Int64")
        # A partir de extract_player_game_logs.py, defense_<ano>.csv já
        # traz uma linha por SEMANA REAL (não mais um total da temporada
        # somado) — o `week` vem direto da própria fonte, sem precisar
        # inferir via team_weeks.
        to_int(defense, ["combined_tackles", "solo_tackles", "assisted_tackles",
                          "safeties", "pass_defended", "week"])
        to_num(defense, ["sacks"])
    else:
        print(f"  [AVISO] {defense_path} não encontrado — colunas de tackle ficarão nulas",
              file=sys.stderr)
        defense = pd.DataFrame(columns=["player_id_team", "season", "week"])

    # interceptions ainda vem da fonte cumulativa (categoria de líderes),
    # então ainda depende de team_weeks (gap conhecido — ver documentação).
    intc = pd.read_csv(stats_dir / f"interceptions_{year}.csv", dtype=str)
    intc = attach_player_id_team(intc, roster, year, team_weeks)
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
                   roster: pd.DataFrame, year: int, team_weeks: dict[str, int]) -> pd.DataFrame:
    df = pd.read_csv(stats_dir / f"fumbles_{year}.csv", dtype=str)
    df = attach_player_id_team(df, roster, year, team_weeks)
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
    parser.add_argument("--stats-dir", required=True,
                         help="saída de extract_category_stats.py (agora só fumbles/"
                              "interceptions/kickoff-returns/punt-returns/downs)")
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

    # team_weeks ainda é necessário pras 4 categorias que continuam
    # cumulativas (fumbles/interceptions/kickoff-returns/punt-returns) —
    # ver docstring do módulo.
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

    print("Construindo kick_return (fonte: categoria, cumulativo — ver seção 10)...")
    build_kick_return(stats_dir, roster, args.year, team_weeks).to_csv(
        out_dir / "kick_return_final.csv", index=False)

    print("Construindo punt_return (fonte: categoria, cumulativo — ver seção 10)...")
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