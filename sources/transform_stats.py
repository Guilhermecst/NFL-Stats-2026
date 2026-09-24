"""
transform_stats.py

Junta os CSVs gerados pelos scripts de extração (roster, downs, logs por
jogador) e produz um CSV final por tabela do banco, já com as colunas
renomeadas para bater com o DDL e prontos para o script de upsert no
Supabase.

Entradas esperadas (ajuste os caminhos via argumentos se necessário):
    players_roster.csv                (extract_players_roster.py)
    logs_dir/games_<ano>.csv          (extract_player_game_logs.py)
    logs_dir/extra_points_<ano>.csv   (idem)
    logs_dir/defense_<ano>.csv        (idem — tackles, interceptions, fumbles forçados/recuperados)
    logs_dir/passing_<ano>.csv        (idem)
    logs_dir/rushing_<ano>.csv        (idem)
    logs_dir/receiving_<ano>.csv      (idem)
    logs_dir/kicking_fg_ko_<ano>.csv  (idem — field goals + kickoffs)
    logs_dir/punting_<ano>.csv        (idem)
    stats_dir/downs_<ano>.csv         (extract_category_stats.py — por time, sem `week`)

Todas as nove tabelas de estatística individual (passing, rushing,
receiving, kicking, punting, defense, fumbles — e games/extra_points,
que alimentam outras) vêm de extract_player_game_logs.py: uma linha por
semana realmente disputada, direto da fonte, sem precisar inferir nada.
`downs` é por time/temporada, sem coluna `week`, e continua vindo da
página de líderes de time (extract_category_stats.py) — sempre foi um
snapshot da temporada por natureza, não uma limitação.

GAPS DE COLUNA: a página de Logs tem menos detalhamento do que a extração
anterior por líderes de categoria tinha, e algumas colunas não têm mais
fonte real — foram removidas do modelo em vez de ficarem permanentemente
zeradas ou vazias (ver DROP COLUMN / DROP TABLE em anexo à documentação
do projeto). Removidas de passing: first_downs, first_down_pct,
pass_20/40_yards_plus, long_gain. De rushing/receiving: first_downs,
first_down_pct, os campos *_20/40_yards_plus, targets (receiving) e
yards_after_catch (receiving). De kicking: o detalhamento de field goal
por faixa de distância (fg_made_1_19 ... fg_att_60_plus), kickoff_yards,
kickoff_return_yards_against, touchback_pct, onside_kicks,
onside_kicks_recovered, kickoffs_out_of_bounds e
kickoff_return_touchdowns_against. De fumbles:
opponent_fumble_recovery_touchdowns. punting é a exceção: nenhuma coluna
foi removida. As tabelas kick_return e punt_return foram removidas do
banco inteiramente — a página de Logs não tem nenhuma coluna de retorno,
para nenhum jogador.

Saídas (em --output-dir): teams_final.csv, players_final.csv,
passing_final.csv, rushing_final.csv, receiving_final.csv,
kicking_final.csv, punting_final.csv, defense_final.csv,
fumbles_final.csv, games_final.csv, downs_final.csv

teams_final.csv: NÃO depende de nenhuma extração (conference/division são
estáticas), só existe pra alimentar a coluna `conf_div` da tabela `teams`
(ex: "NFC East") via upsert em load_to_supabase.py — as demais colunas de
`teams` continuam vindo do seed original, fora deste pipeline.

GAP CONHECIDO / LIMITAÇÃO DA FONTE: a página de Logs não separa
estatísticas por time — um jogador negociado no meio da temporada aparece
com a produção da semana sob o TIME ATUAL, mesmo que o jogo tenha sido
pelo time anterior. Isso é uma limitação do próprio nfl.com, não deste
script: o `player_id_team` é sempre montado com o time do roster mais
recente.

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
    jogos desta rodada — não um único número global. Usado só para o
    aviso de execução parcial em main(): como cada tabela de estatística
    individual já traz `week` direto da própria fonte (extract_player_
    game_logs.py), essa informação não alimenta mais nenhum build_*, só
    o diagnóstico impresso no log."""
    df = pd.read_csv(games_path, dtype=str)
    df["week"] = pd.to_numeric(df["week"], errors="coerce")
    df = df.dropna(subset=["week"])
    if df.empty:
        raise ValueError(f"Não foi possível determinar a semana atual a partir de {games_path}")
    return df.groupby("team_id")["week"].max().astype(int).to_dict()


def split_att_made(df: pd.DataFrame, col: str, made_col: str, att_col: str) -> pd.DataFrame:
    """Converte uma coluna tipo '8/8' (feito/tentado) em duas colunas
    numéricas. Não usada por nenhum build_* atualmente — nenhuma fonte
    em uso hoje traz esse formato. Mantida por se um dia outra fonte com
    esse mesmo formato precisar dela."""
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
    """Tabela `teams`, só a coluna `conf_div` (ex: "NFC East"). Não
    depende de nenhum CSV de extração — vem de TEAM_CONF_DIV, um mapa
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
    """Cada linha já é uma semana real, direto de extract_player_game_
    logs.py."""
    cols = ["player_id_team", "season", "week", "yards", "yards_per_attempt", "attempts", "completions",
            "completion_pct", "touchdowns", "interceptions", "rate", "sacks", "sacks_yards"]
    if not passing_path.exists():
        print(f"  [AVISO] {passing_path} não encontrado — passing ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(passing_path, dtype=str)
    to_num(df, ["yards_per_attempt", "rate", "completion_pct"])
    to_int(df, ["season", "week", "yards", "attempts", "completions", "touchdowns",
                "interceptions", "sacks", "sacks_yards"])
    return df[cols]


def build_rushing(rushing_path: Path) -> pd.DataFrame:
    """fumbles vem do par FUM/LOST único da página de Logs (ver docstring
    de extract_player_game_logs.py: aproximação quando o jogador também
    tem bloco de recepção na mesma semana)."""
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
    """Mesma aproximação de fumbles de build_rushing."""
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


def build_punting(punting_path: Path) -> pd.DataFrame:
    """A página de Logs de um punter tem todas as colunas que a tabela
    precisa — sem gap nenhum aqui."""
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
    """Junta field goals + kickoffs com extra points — as duas fontes vêm
    de extract_player_game_logs.py, unidas por (player_id_team, season,
    week)."""
    cols = ["player_id_team", "season", "week", "field_goals_made", "field_goal_attempts",
            "field_goal_pct", "field_goal_long", "field_goals_blocked",
            "kickoffs", "touchbacks", "kickoff_returns_against", "kickoff_return_avg_against",
            "extra_point_attempts", "extra_points_made", "extra_point_pct", "extra_points_blocked"]

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

    # o merge "outer" introduz NaN quando um kicker só aparece em uma das
    # duas fontes numa dada semana — corrige depois do merge, não só antes.
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


def build_defense(defense_path: Path) -> pd.DataFrame:
    """Tackles, interceptions (com jardas/retorno mais longo/touchdown de
    retorno) e, indiretamente via build_fumbles, fumbles forçados/
    recuperados vêm todos da mesma linha de extract_player_game_logs.py
    — uma linha por semana real, sem precisar de roster/team_weeks nem
    merge com outra fonte."""
    cols = ["player_id_team", "season", "week", "combined_tackles", "solo_tackles",
            "assisted_tackles", "sacks", "safeties", "pass_defended", "interceptions",
            "interception_yards", "interception_long", "interception_touchdowns"]
    if not defense_path.exists():
        print(f"  [AVISO] {defense_path} não encontrado — defense ficará vazio", file=sys.stderr)
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(defense_path, dtype=str)
    to_int(df, ["season", "week", "combined_tackles", "solo_tackles", "assisted_tackles",
                "safeties", "pass_defended", "interceptions", "interception_yards",
                "interception_long", "interception_touchdowns"])
    to_num(df, ["sacks"])
    return df[cols]


def build_fumbles(defense_path: Path, rushing: pd.DataFrame, receiving: pd.DataFrame) -> pd.DataFrame:
    """forced_fumbles e opponent_fumbles_recovered vêm do mesmo CSV de
    defense (colunas FF/FR de extract_player_game_logs.py) — uma linha
    por semana real. own_fumbles é a soma dos fumbles cometidos já
    extraídos para rushing e receiving."""
    cols = ["player_id_team", "season", "week", "forced_fumbles",
            "opponent_fumbles_recovered", "own_fumbles"]

    if defense_path.exists():
        d = pd.read_csv(defense_path, dtype=str)
        d = d.rename(columns={"fumbles_forced": "forced_fumbles", "fumbles_recovered": "opponent_fumbles_recovered"})
        to_int(d, ["season", "week", "forced_fumbles", "opponent_fumbles_recovered"])
        keep = ["player_id_team", "season", "week", "forced_fumbles", "opponent_fumbles_recovered"]
        d = d[[c for c in keep if c in d.columns]]
    else:
        print(f"  [AVISO] {defense_path} não encontrado — forced_fumbles/"
              f"opponent_fumbles_recovered ficarão zerados", file=sys.stderr)
        d = pd.DataFrame(columns=["player_id_team", "season", "week",
                                   "forced_fumbles", "opponent_fumbles_recovered"])

    own = rushing[["player_id_team", "season", "week", "fumbles"]].rename(columns={"fumbles": "rush_fum"})
    own = own.merge(
        receiving[["player_id_team", "season", "week", "fumbles"]].rename(columns={"fumbles": "rec_fum"}),
        on=["player_id_team", "season", "week"], how="outer",
    )
    own["rush_fum"] = own["rush_fum"].fillna(0)
    own["rec_fum"] = own["rec_fum"].fillna(0)
    own["own_fumbles"] = own["rush_fum"] + own["rec_fum"]
    own = own[["player_id_team", "season", "week", "own_fumbles"]]

    merged = d.merge(own, on=["player_id_team", "season", "week"], how="outer")
    for c in ["forced_fumbles", "opponent_fumbles_recovered", "own_fumbles"]:
        merged[c] = merged[c].fillna(0).astype(int)
    return merged[cols]


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
                         help="saída de extract_category_stats.py (só downs)")
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

    team_weeks = determine_team_weeks(games_path)
    weeks_found = sorted(set(team_weeks.values()))
    if len(weeks_found) > 1:
        behind = sorted(t for t, w in team_weeks.items() if w < weeks_found[-1])
        print(f"[AVISO] Execução parcial: {len(behind)} time(s) ainda na semana "
              f"{weeks_found[0]} enquanto outros já estão na semana {weeks_found[-1]}: "
              f"{behind}.", file=sys.stderr)
    else:
        print(f"Semana atual detectada (todos os times): {weeks_found[0]}")

    print("Construindo players...")
    build_players(roster, args.year).to_csv(out_dir / "players_final.csv", index=False)

    print("Construindo passing...")
    build_passing(passing_path).to_csv(out_dir / "passing_final.csv", index=False)

    print("Construindo rushing...")
    rushing = build_rushing(rushing_path)
    rushing.to_csv(out_dir / "rushing_final.csv", index=False)

    print("Construindo receiving...")
    receiving = build_receiving(receiving_path)
    receiving.to_csv(out_dir / "receiving_final.csv", index=False)

    print("Construindo punting...")
    build_punting(punting_path).to_csv(out_dir / "punting_final.csv", index=False)

    print("Construindo kicking (FG+KO+XP)...")
    build_kicking(kicking_fg_ko_path, extra_points_path).to_csv(
        out_dir / "kicking_final.csv", index=False)

    print("Construindo defense (tackles + interceptions)...")
    build_defense(defense_logs_path).to_csv(out_dir / "defense_final.csv", index=False)

    print("Construindo fumbles (forçados/recuperados + próprio)...")
    build_fumbles(defense_logs_path, rushing, receiving).to_csv(
        out_dir / "fumbles_final.csv", index=False)

    print("Construindo games...")
    build_games(games_path).to_csv(out_dir / "games_final.csv", index=False)

    print("Construindo downs...")
    build_downs(stats_dir, args.year).to_csv(out_dir / "downs_final.csv", index=False)

    print(f"\nConcluído. CSVs finais em: {out_dir}/")


if __name__ == "__main__":
    main()