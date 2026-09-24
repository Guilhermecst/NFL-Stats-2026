"""
load_to_supabase.py

Fase 4 do pipeline: faz upsert (INSERT ... ON CONFLICT DO UPDATE) dos CSVs
finais (gerados por transform_stats.py) nas tabelas do Supabase. Exceção:
`teams` usa um UPDATE puro, não upsert — ver comentário em TABLES.

IMPORTANTE: é upsert, não insert puro. Toda semana os jogadores acumulam
mais estatísticas (jogos, jardas, etc.) e os números MUDAM — não são
somados nem duplicados. Cada linha do CSV substitui os valores da linha
existente (mesma chave primária) pelos valores mais recentes; se a chave
ainda não existir, insere. Rodar este script todo santo dia da semana com
o mesmo player_id_team+season é seguro (idempotente).

Ordem de carga (respeita as FKs do DDL):
    1. teams                (só atualiza a coluna conf_div; a linha de cada
                              time já existe via seed — demais colunas de
                              `teams` não são tocadas por este upsert)
    2. players             (referenciada por todas as tabelas individuais)
    3. passing, rushing, receiving, kicking, punting, defense, fumbles
       (FK -> players)
    4. games                (FK -> teams, que já foi populada pelo seed)

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
    # aponta explicitamente para o .env na mesma pasta deste script — assim
    # funciona não importa de onde você rode o comando (não depende do
    # diretório atual do terminal)
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv é opcional; em produção (GitHub Actions) a env var já vem pronta

# (nome_da_tabela, arquivo_csv, colunas_da_chave_primaria, update_only)
# `teams` vem primeiro: só carrega `conf_div` (ex: "NFC East") por cima das
# linhas já existentes (seed) — a chave primária é só team_id (não varia
# por temporada). Precisa vir antes de `games`, que tem FK para `teams`.
# `update_only=True` faz um UPDATE puro (sem INSERT ... ON CONFLICT): o CSV
# de teams só tem team_id + conf_div, então um INSERT de linha nova
# quebraria as colunas NOT NULL da tabela (team_name, etc.) que não vêm
# desta fonte — a linha do time tem que já existir via seed. Se ainda não
# existir, o UPDATE simplesmente não afeta nenhuma linha (0), sem erro.
TABLES = [
    ("teams", "teams_final.csv", ["team_id"], True),
    ("players", "players_final.csv", ["player_id_team", "season"], False),
    ("passing", "passing_final.csv", ["player_id_team", "season", "week"], False),
    ("rushing", "rushing_final.csv", ["player_id_team", "season", "week"], False),
    ("receiving", "receiving_final.csv", ["player_id_team", "season", "week"], False),
    ("kicking", "kicking_final.csv", ["player_id_team", "season", "week"], False),
    ("punting", "punting_final.csv", ["player_id_team", "season", "week"], False),
    ("defense", "defense_final.csv", ["player_id_team", "season", "week"], False),
    ("fumbles", "fumbles_final.csv", ["player_id_team", "season", "week"], False),
    ("games", "games_final.csv", ["team_id", "season", "week"], False),
    ("downs", "downs_final.csv", ["team_id", "season"], False),
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


def upsert_table(conn, table: str, csv_path: Path, pk_columns: list[str], update_only: bool = False):
    if not csv_path.exists():
        print(f"  [AVISO] {csv_path} não encontrado, pulando tabela {table}", file=sys.stderr)
        return 0

    columns, rows = read_csv_rows(csv_path)
    if not rows:
        print(f"  [AVISO] {csv_path} está vazio, pulando tabela {table}", file=sys.stderr)
        return 0

    missing_pk = [c for c in pk_columns if c not in columns]
    if missing_pk:
        raise ValueError(
            f"{csv_path} não tem a(s) coluna(s) de chave primária {missing_pk} "
            f"esperada(s) para a tabela {table}. Isso normalmente significa que o "
            f"CSV é de uma versão antiga do transform_stats.py — rode o "
            f"transform_stats.py de novo para gerar um final_<ano>/ atualizado "
            f"antes de rodar a carga."
        )

    update_columns = [c for c in columns if c not in pk_columns]

    if update_only:
        # UPDATE puro via VALUES: nunca insere linha nova. Usado para CSVs
        # "parciais" (como teams_final.csv, que só tem team_id + conf_div)
        # que não têm dados suficientes pra satisfazer as colunas NOT NULL
        # de um INSERT — a linha-alvo precisa já existir (via seed). Se não
        # existir, a linha do CSV simplesmente não casa com nada e não
        # afeta nenhuma linha (sem erro).
        if not update_columns:
            print(f"  [AVISO] {table} não tem colunas além da chave primária, nada a atualizar",
                  file=sys.stderr)
            return 0

        col_list = ", ".join(columns)  # ordem das colunas no VALUES, sem aspas (alias)
        set_clause = ", ".join(f'"{c}" = data."{c}"' for c in update_columns)
        where_clause = " AND ".join(f'{table}."{c}" = data."{c}"' for c in pk_columns)
        extra_updated_at = ', "updated_at" = now()' if "updated_at" not in columns else ""

        query = (
            f'UPDATE {SCHEMA}."{table}" SET {set_clause}{extra_updated_at} '
            f'FROM (VALUES %s) AS data({col_list}) '
            f'WHERE {where_clause}'
        )

        with conn.cursor() as cur:
            execute_values(cur, query, rows, page_size=500)
            affected = cur.rowcount
        conn.commit()

        if affected < len(rows):
            print(f"  [AVISO] {len(rows) - affected} linha(s) de {csv_path.name} não "
                  f"encontraram uma linha correspondente em {table} (ainda não existe? "
                  f"rode o seed primeiro)", file=sys.stderr)

        return affected

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

    # força saída linha-a-linha mesmo quando o processo roda com stdout
    # redirecionado (caso do GitHub Actions) — sem isso, os prints ficam
    # em buffer e só aparecem no log de uma vez, misturados de forma
    # confusa com o traceback (que vai direto pro stderr, sem buffer).
    sys.stdout.reconfigure(line_buffering=True)

    if not args.conn_string:
        print("[ERRO] informe --conn-string ou defina a variável de ambiente "
              "SUPABASE_DB_URL", file=sys.stderr)
        sys.exit(1)

    final_dir = Path(args.final_dir)

    conn = psycopg2.connect(args.conn_string)
    try:
        for table, filename, pk_columns, update_only in TABLES:
            csv_path = final_dir / filename
            verbo = "Update" if update_only else "Upsert"
            print(f"{verbo} em {SCHEMA}.{table} <- {filename} ...")
            count = upsert_table(conn, table, csv_path, pk_columns, update_only)
            print(f"  -> {count} linhas processadas")
    finally:
        conn.close()

    print("\nConcluído.")


if __name__ == "__main__":
    main()