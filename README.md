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
    ├── extract_category_stats.py      Season leaders by category (fumbles, interceptions, kickoff/punt returns) + team downs
    ├── extract_player_game_logs.py    Per-player weekly logs: games, passing, rushing, receiving, kicking, punting, extra points, defense (tackles)
    ├── transform_stats.py             Joins and reshapes all extracted CSVs into the final table format
    └── load_to_supabase.py            Upserts the final CSVs into Supabase
```

SQL files used to build and maintain the database schema (not part of the automated pipeline, run manually against Supabase):

```
ddl_nfl_2026.sql                    Full schema (fresh install)
seed_teams_positions.sql            Static reference data: teams and positions
migration_fix_kicking_punting.sql   Early fix aligning Kicking/Punting to nfl.com fields
migration_v2_category_stats.sql     Schema rewrite for the category-leaderboard extraction method
fix_missing_positions.sql           Adds position codes missing from the original seed
fix_punting_columns.sql             Adds/renames columns on an already-existing punting table
drop_empty_columns.sql              Drops 34 columns with no real weekly source (see Known limitations)
```

## Database

Schema: `nfl`, hosted on Supabase (PostgreSQL).

**Reference tables** (static, seeded once): `teams` (32 teams, including a `conf_div` column combining conference and division, e.g. "NFC East", plus colors and logos), `positions` (position codes grouped into Offense, Defense, Special Team). `teams` is update-only in `load_to_supabase.py` — it is never inserted into, only updated, since the weekly pipeline only ever refreshes `conf_div` and never has the full set of columns a fresh insert would need.

**Dimension**: `players`, keyed by `(player_id_team, season)`. `player_id_team` combines the player's slug and current team abbreviation (for example `brock-purdy-SF`), so that a player traded mid-season does not have his statistics mixed between his former and current team in the database.

**Individual statistics tables** (weekly snapshots, upserted every run): `passing`, `rushing`, `receiving`, `kicking`, `kick_return`, `punt_return`, `punting`, `defense`, `fumbles`. All keyed by `(player_id_team, season, week)`, so every week's extraction adds a new row instead of overwriting the previous week's. `week` is inferred per team from that team's own most recently played game, not from a single league-wide week number — this avoids mid-week runs stamping teams that haven't played yet with a future week's number.

**Team statistics tables**: `games` (one row per team per week, keyed by `(team_id, season, week)`), plus `downs`, keyed by `(team_id, season)` — `downs` has no weekly history yet, only a season-to-date snapshot.

A `season` column was added to every table so the database can hold multiple NFL seasons without overwriting prior data — this was not part of the original data model and was introduced during the design phase.

## Data sources and extraction strategy

nfl.com does not expose a single endpoint with all the data needed, so the pipeline combines three page types:

- **Team rosters** (`nfl.com/teams/<team>/roster`): one page per team, 32 requests total. This is the source of truth for which players exist and which team they currently belong to. Because it reflects the current roster, retirements and new signings are handled automatically — there is no static player list to maintain.
- **Per-player season logs** (`nfl.com/players/<slug>/stats/logs/<year>/`): one row per week actually played, for essentially the entire active roster (QB, RB/FB/WR/TE, K, P, and defensive positions). This is the primary source now — it is the only nfl.com page that reports a real per-week breakdown rather than a running season total, and it feeds `games`, `passing`, `rushing`, `receiving`, `kicking` (field goals + kickoffs + extra points), `punting`, and `defense` (tackles). All of this is collected by a single script, `extract_player_game_logs.py`, one request per player.
- **Season leaders by category** (`nfl.com/stats/player-stats/category/<category>/...`): only four categories remain here — Fumbles, Interceptions, Kickoff Returns, Punt Returns — plus a team-level Downs leaderboard. These stayed on this source because the per-player logs page has no equivalent columns for them (see Known limitations). Pagination uses an opaque cursor (`aftercursor` query parameter) followed via a "Next Page" link.

`extract_qb_games.py`, `extract_extra_points.py`, and `extract_defense_stats.py` (three separate scripts that used to cover this ground one page-type at a time) were consolidated into `extract_player_game_logs.py`, since they all scraped the same per-player logs page — this also cut duplicate HTTP requests to the same player pages.

## Known limitations

- **Four stat categories are still season-cumulative, not weekly**: `kick_return`, `punt_return`, `interceptions`, and `fumbles` are still sourced from the category-leaderboard pages, which only ever report a running season total, not a per-week breakdown. This is a known, unresolved gap — the per-player logs page has no return-yardage columns at all, and its FUM/LOST and defensive blocks don't split cleanly into what the leaderboard categories track.
- **34 columns were removed from the schema, not zero-filled**: when `passing`, `rushing`, `receiving`, and `kicking` moved to the per-player logs source, several columns the leaderboard pages used to report (first downs, 20+/40+ yard plays, `targets`, `yards_after_catch`, field-goal distance buckets, most kickoff detail, etc.) had no equivalent on the logs page. Zero-filling them, or recomputing them by subtracting one week's leaderboard total from the next, were both considered and rejected: either approach only gives correct results for someone who has run the pipeline continuously since week 1, not for someone rebuilding the database from scratch mid-season — which the pipeline is meant to support for anyone who uses this code, at any point in the season. The columns were dropped from the model instead (`drop_empty_columns.sql`), including `touchback_pct`, the one column that actually was recoverable by subtraction, for the same reproducibility reason.
- **Tackles category unavailable**: nfl.com's own "Tackles" leaderboard page returns "No Stats Available" regardless of season or sort order, which is why tackle/sack totals are collected from the per-player logs page instead of a leaderboard.
- **Mid-season trades**: `player_id_team` is derived from the player's roster page at extraction time, so a player traded mid-season is attributed to his current team for the week the trade takes effect; his stats from before the trade stay associated with his former team's `player_id_team`.

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` (in the same folder as the scripts) and fill in the Supabase Postgres connection string (Supabase dashboard: Project Settings > Database > Connection string > URI).
3. Run the DDL and seed scripts against the Supabase database (SQL Editor in the Supabase dashboard, in order: `ddl_nfl_2026.sql`, then `seed_teams_positions.sql`). On a database created before the column removal described above, also run `drop_empty_columns.sql`.

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

`load_to_supabase.py` performs an upsert (`INSERT ... ON CONFLICT DO UPDATE`) keyed on each table's primary key — `teams` is the one exception, loaded with a plain `UPDATE` instead (see Database, above). Because the individual statistics tables are now keyed by `(player_id_team, season, week)`, running the pipeline again in a later week adds new rows for the new week instead of overwriting the previous week's data.

Both extraction scripts fail loudly (non-zero exit code, with a diagnostic summary) if they come back with zero rows for something they expected data on, instead of silently skipping the output file and letting a later step crash with an unrelated-looking error.

## Automation

`.github/workflows/weekly_extraction.yml` runs the entire pipeline above, in order, every Tuesday at 09:00 UTC — after the previous round's Monday Night Football game has finished — and can also be triggered manually from the Actions tab. Extracted and transformed CSVs are uploaded as a workflow artifact (kept for 14 days) on every run, successful or not, to help diagnose failures without having to reproduce them locally.
