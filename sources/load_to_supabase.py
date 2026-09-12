"""
load_to_supabase.py

Fase 4 do pipeline: faz upsert (INSERT ... ON CONFLICT DO UPDATE) dos CSVs
finais (gerados por transform_stats.py) nas tabelas do Supabase.

IMPORTANTE: é upsert, não insert puro. Toda semana os jogadores acumulam
mais estatísticas (jogos, jardas, etc.) e os números MUDAM — não são
somados nem duplicados. Cada linha do CSV substitui os valores da linha
existente (mesma chave primária) pelos valores mais recentes; se a chave
ainda não existir, insere. Rodar este script todo santo dia da semana com
o mesmo player_id_team+season é seguro (idempotente).

Ordem de carga (respeita as FKs do DDL):
    1. players             (referenciada por todas as tabelas individuais)
    2. passing, rushing, receiving, kicking, kick_return, punt_return,
       punting, defense, fumbles   (FK -> players)
    3. games                (FK -> teams, que já foi populada pelo seed)

Conexão: string de conexão do Postgres do Supabase, via variável de
ambiente SUPABASE_DB_URL (Project Settings > Database > Connection string
> URI, no painel do Supabase) ou por --conn-string.

Uso:
    export SUPABASE_DB_URL="postgresql://postgres:<senha>@<host>:5432/postgres"
    python load_to_supabase.py --final-dir ./final_2026
"""

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

try:
    from dotenv import load_dotenv
    load_dotenv()  # lê um arquivo .env local, se existir (nunca commitado — ver .gitignore)
except ImportError:
    pass  # python-dotenv é opcional; em produção (GitHub Actions) a env var já vem pronta

# (nome_da_tabela, arquivo_csv, colunas_da_chave_primaria)
TABLES = [
    ("players", "players_final.csv", ["player_id_team", "season"]),
    ("passing", "passing_final.csv", ["player_id_team", "season"]),
    ("rushing", "rushing_final.csv", ["player_id_team", "season"]),
    ("receiving", "receiving_final.csv", ["player_id_team", "season"]),
    ("kicking", "kicking_final.csv", ["player_id_team", "season"]),
    ("kick_return", "kick_return_final.csv", ["player_id_team", "season"]),
    ("punt_return", "punt_return_final.csv", ["player_id_team", "season"]),
    ("punting", "punting_final.csv", ["player_id_team", "season"]),
    ("defense", "defense_final.csv", ["player_id_team", "season"]),
    ("fumbles", "fumbles_final.csv", ["player_id_team", "season"]),
    ("games", "games_final.csv", ["team_id", "season", "week"]),
]

SCHEMA = "nfl"


def read_csv_rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
        rows = []
        for row in reader:
            # célula vazia -> NULL (None) em vez de string vazia
            rows.append([row[c] if row[c] not in ("", None) else None for c in columns])
    return columns, rows


def upsert_table(conn, table: str, csv_path: Path, pk_columns: list[str]):
    if not csv_path.exists():
        print(f"  [AVISO] {csv_path} não encontrado, pulando tabela {table}", file=sys.stderr)
        return 0

    columns, rows = read_csv_rows(csv_path)
    if not rows:
        print(f"  [AVISO] {csv_path} está vazio, pulando tabela {table}", file=sys.stderr)
        return 0

    update_columns = [c for c in columns if c not in pk_columns]

    col_list = ", ".join(f'"{c}"' for c in columns)
    pk_list = ", ".join(f'"{c}"' for c in pk_columns)
    set_clause = ", ".join(f'"{c}" = excluded."{c}"' for c in update_columns)
    # updated_at (quando existir na tabela) sempre reflete o momento da carga
    if "updated_at" not in update_columns and "updated_at" in columns:
        pass  # já incluída via update_columns se vier do CSV
    extra_updated_at = ', "updated_at" = now()' if "updated_at" not in columns else ""

    query = (
        f'INSERT INTO {SCHEMA}."{table}" ({col_list}) VALUES %s '
        f'ON CONFLICT ({pk_list}) DO UPDATE SET {set_clause}{extra_updated_at}'
    )

    with conn.cursor() as cur:
        execute_values(cur, query, rows, page_size=500)
    conn.commit()

    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-dir", required=True,
                         help="diretório com os CSVs gerados por transform_stats.py")
    parser.add_argument("--conn-string", default=os.environ.get("SUPABASE_DB_URL"),
                         help="connection string do Postgres (ou defina SUPABASE_DB_URL)")
    args = parser.parse_args()

    if not args.conn_string:
        print("[ERRO] informe --conn-string ou defina a variável de ambiente "
              "SUPABASE_DB_URL", file=sys.stderr)
        sys.exit(1)

    final_dir = Path(args.final_dir)

    conn = psycopg2.connect(args.conn_string)
    try:
        for table, filename, pk_columns in TABLES:
            csv_path = final_dir / filename
            print(f"Upsert em {SCHEMA}.{table} <- {filename} ...")
            count = upsert_table(conn, table, csv_path, pk_columns)
            print(f"  -> {count} linhas processadas")
    finally:
        conn.close()

    print("\nConcluído.")


if __name__ == "__main__":
    main()