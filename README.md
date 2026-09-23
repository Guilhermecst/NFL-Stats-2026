# NFL 2026 Stats Pipeline

A data pipeline that extracts weekly player and team statistics for the NFL 2026 regular season directly from nfl.com, stores them in a Supabase (Postgres) database, and runs automatically on a weekly schedule via GitHub Actions.

The database is designed as the foundation for a Power BI dashboard and a playoff prediction model built with machine learning. This repository currently covers the extraction, transformation, and storage layer.

## Repository structure

```
NFL-Stats-2026/
├── .github/
│   └── workflows/
│       └── weekly_extraction.yml     GitHub Actions workflow (weekly run)
├── requirements.txt                   Python dependencies
├── .env.example                       Template for local environment variables
├── .gitignore
└── sources/
    ├── extract_players_roster.py      Roster scraper (32 teams)
    ├── extract_category_stats.py      Downs, the only remaining team stats leaders page
    ├── extract_player_game_logs.py    Per player season logs, source for every individual stats table
    ├── transform_stats.py             Joins and reshapes all extracted CSVs into the final table format
    └── load_to_supabase.py            Upserts the final CSVs into Supabase
```

SQL files used to build and maintain the database schema (not part of the automated pipeline, run manually against Supabase):

```
ddl_nfl_2026.sql                    Full schema (fresh install)
seed_teams_positions.sql            Static reference data: teams and positions
migration_fix_kicking_punting.sql   Early fix aligning Kicking/Punting to nfl.com fields
migration_v2_category_stats.sql     Schema rewrite for the category leaderboard extraction method
fix_missing_positions.sql           Adds position codes missing from the original seed
fix_punting_columns.sql             Adds/renames columns on an already existing punting table
drop_empty_columns.sql              Drops 34 columns with no real weekly source, from passing/rushing/receiving/kicking
drop_return_tables.sql              Drops kick_return and punt_return, and fumbles.opponent_fumble_recovery_touchdowns
```

## Database

Schema: `nfl`, hosted on Supabase (PostgreSQL).

**Reference tables** (static, seeded once): `teams` (32 teams, including a `conf_div` column combining conference and division, e.g. "NFC East", plus colors and logos), `positions` (position codes grouped into Offense, Defense, Special Team). `teams` is update only in `load_to_supabase.py`, never inserted into, since the weekly pipeline only ever refreshes `conf_div` and never has the full set of columns a fresh insert would need.

**Dimension**: `players`, keyed by `(player_id_team, season)`. `player_id_team` combines the player's slug and current team abbreviation (for example `brock-purdy-SF`), so that a player traded mid season does not have his statistics mixed between his former and current team in the database.

**Individual statistics tables** (weekly snapshots, upserted every run): `passing`, `rushing`, `receiving`, `kicking`, `punting`, `defense`, `fumbles`. All keyed by `(player_id_team, season, week)`, so every week's extraction adds a new row instead of overwriting the previous week's. `week` comes directly from the source for every one of these tables now, since the per player logs page already reports one row per week actually played.

**Team statistics tables**: `games` (one row per team per week, keyed by `(team_id, season, week)`), plus `downs`, keyed by `(team_id, season)`. `downs` has no weekly history at the source; it is a season to date snapshot by definition (third/fourth down conversions, first downs by type, scrimmage plays), so that snapshot is the correct value.

A `season` column was added to every table so the database can hold multiple NFL seasons without overwriting prior data. This was not part of the original data model and was introduced during the design phase.

## Data sources and extraction strategy

nfl.com does not expose a single endpoint with all the data needed, so the pipeline combines three page types:

- **Team rosters** (`nfl.com/teams/<team>/roster`): one page per team, 32 requests total. This is the source of truth for which players exist and which team they currently belong to. Because it reflects the current roster, retirements and new signings are handled automatically; there is no static player list to maintain.
- **Per player season logs** (`nfl.com/players/<slug>/stats/logs/<year>/`): the single source for every individual statistics table. One row per week actually played (bye weeks are correctly absent), for essentially the entire active roster (QB, RB/FB/WR/TE, K, P, and every defensive position). This page feeds `games`, `passing`, `rushing`, `receiving`, `kicking` (field goals, kickoffs, and extra points), `punting`, `defense` (tackles, sacks, safeties, passes defended, interceptions with return detail, and forced/recovered fumbles), and `fumbles`. Several stat blocks on the page reuse the same column names, so the script locates each block by its position within the header row rather than by an isolated column name. All of this is collected by a single script, `extract_player_game_logs.py`, one request per player.
- **Team stats leaders** (`nfl.com/stats/team-stats/offense/downs/<year>/reg/all`): used only for `downs`, the one team table with no equivalent at the player level. It is a single request covering all 32 teams, with no pagination. `extract_category_stats.py` is the script for this; it used to cover ten player leaderboard categories, but everything except downs has since migrated to the per player logs page (see Design history, below).

## Known limitations

- **No return statistics**: the per player logs page has no kickoff or punt return column for any player, confirmed against the page of an actual starting returner. `kick_return` and `punt_return` were part of the original data model but were removed entirely, since there was no other real source to populate them consistently with the rest of the pipeline (see `drop_return_tables.sql`).
- **35 columns were removed from the schema, not zero filled**: when the pipeline moved to the per player logs page, several columns the old category leaderboard pages used to report had no equivalent there: first downs, 20+/40+ yard plays, and long gain in passing/rushing/receiving; `targets` and `yards_after_catch` in receiving; field goal distance buckets and most kickoff detail beyond the basics in kicking; and `opponent_fumble_recovery_touchdowns` in fumbles. Zero filling them, or recomputing them by subtracting one week's leaderboard total from the next, were both considered and rejected: either approach only gives correct results for someone who has run the pipeline continuously since week 1, not for someone rebuilding the database from scratch mid season, which the pipeline is meant to support for anyone who uses this code, at any point in the season. The columns were dropped from the model instead (`drop_empty_columns.sql`, `drop_return_tables.sql`), including `touchback_pct`, the one column that actually was recoverable by subtraction, for the same reproducibility reason.
- **Fumbles committed are approximate**: the logs page reports a single pair of fumble columns for offensive plays without separating rushing from receiving. The pipeline assigns that pair to receiving when the week's row has receiving production, otherwise to rushing.
- **Mid season trades**: `player_id_team` is derived from the player's roster page at extraction time, so a player traded mid season is attributed to his current team going forward; his stats from before the trade stay associated with his former team's `player_id_team`.

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` (in the same folder as the scripts) and fill in the Supabase Postgres connection string (Supabase dashboard: Project Settings > Database > Connection string > URI).
3. Run the DDL and seed scripts against the Supabase database (SQL Editor in the Supabase dashboard, in order: `ddl_nfl_2026.sql`, then `seed_teams_positions.sql`). On a database created before the column and table removals described above, also run `drop_empty_columns.sql` and `drop_return_tables.sql`.

Credentials are never hardcoded. Locally they are read from `.env` (excluded from version control by `.gitignore`); in GitHub Actions they are read from a repository secret (`SUPABASE_DB_URL`).

## Running the pipeline manually

```
cd sources
python extract_players_roster.py --output players_roster.csv
python extract_category_stats.py --year 2026 --output-dir stats_2026
python extract_player_game_logs.py --roster players_roster.csv --year 2026 --output-dir logs_2026
python transform_stats.py --year 2026 --roster players_roster.csv \
    --stats-dir stats_2026 --logs-dir logs_2026 \
    --output-dir final_2026
python load_to_supabase.py --final-dir final_2026
```

`load_to_supabase.py` performs an upsert (`INSERT ... ON CONFLICT DO UPDATE`) keyed on each table's primary key; `teams` is the one exception, loaded with a plain `UPDATE` instead (see Database, above). Because the individual statistics tables are keyed by `(player_id_team, season, week)`, running the pipeline again in a later week adds new rows for the new week instead of overwriting the previous week's data.

`extract_player_game_logs.py` fails loudly (non-zero exit code, with a diagnostic summary) if it comes back with zero games collected, instead of silently skipping its output files and letting a later step crash with an unrelated looking error. `transform_stats.py` tolerates a missing source CSV for any single table (logs a warning and continues with that table empty) rather than stopping the whole pipeline over it.

## Automation

`.github/workflows/weekly_extraction.yml` runs the entire pipeline above, in order, every Tuesday at 09:00 UTC, timed to run after the previous round's Monday Night Football game has finished, and can also be triggered manually from the Actions tab. Extracted and transformed CSVs are uploaded as a workflow artifact (kept for 14 days) on every run, successful or not, to help diagnose failures without having to reproduce them locally.

## Design history

The schema and extraction strategy went through several rounds of correction as the pipeline was checked against what nfl.com actually publishes and as reproducibility requirements were worked out: the database has to return the same result for anyone rebuilding it from scratch at any point in the season, not only for someone who has been running it every week since the start. That includes an earlier migration to per category leaderboard pages, then a larger migration to the per player logs page used today, and finally moving interceptions and forced/recovered fumbles onto that same logs page and removing `kick_return`, `punt_return`, and 35 other columns with no real weekly source. The full account is in `NFL_2026_Pipeline_Documentacao_PT.docx` (Portuguese) and `NFL_2026_Pipeline_Documentation_EN.docx` (English).
