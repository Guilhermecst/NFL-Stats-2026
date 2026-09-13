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
    ├── extract_category_stats.py      Season leaders by category (10 categories)
    ├── extract_qb_games.py            Game-by-game results, via QB season logs
    ├── extract_extra_points.py        Extra point stats, via kicker season logs
    ├── extract_defense_stats.py       Tackles/sacks/safeties, via defender season logs
    ├── transform_stats.py             Joins and reshapes all extracted CSVs
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
```

## Database

Schema: `nfl`, hosted on Supabase (PostgreSQL).

**Reference tables** (static, seeded once): `teams` (32 teams with conference, division, colors, logos), `positions` (position codes grouped into Offense, Defense, Special Team).

**Dimension**: `players`, keyed by `(player_id_team, season)`. `player_id_team` combines the player's slug and current team abbreviation (for example `brock-purdy-SF`), so that a player traded mid-season does not have his statistics mixed between his former and current team in the database.

**Individual statistics tables** (season-cumulative, upserted weekly): `passing`, `rushing`, `receiving`, `kicking`, `kick_return`, `punt_return`, `punting`, `defense`, `fumbles`. All keyed by `(player_id_team, season)`.

**Team statistics tables**: `games` (one row per team per week, keyed by `(team_id, season, week)`), plus `downs`, `passes`, `extra_points`, `tackles` at the team level.

A `season` column was added to every table so the database can hold multiple NFL seasons without overwriting prior data — this was not part of the original data model and was introduced during the design phase.

## Data sources and extraction strategy

nfl.com does not expose a single endpoint with all the data needed, so the pipeline combines four different page types:

- **Team rosters** (`nfl.com/teams/<team>/roster`): one page per team, 32 requests total. This is the source of truth for which players exist and which team they currently belong to. Because it reflects the current roster, retirements and new signings are handled automatically — there is no static player list to maintain.
- **Season leaders by category** (`nfl.com/stats/player-stats/category/<category>/...`): ten categories (Passing, Rushing, Receiving, Fumbles, Interceptions, Field Goals, Kickoffs, Kickoff Returns, Punting, Punt Returns), each listing every player with a statistic in that category for the season, already aggregated. Pagination uses an opaque cursor (`aftercursor` query parameter) followed via a "Next Page" link.
- **QB season logs** (`nfl.com/players/<slug>/stats/logs/<year>/`): used only to populate the `games` table (week, opponent, home/away, score). Every quarterback on a team's roster is scraped, not just the presumed starter, so that the full season is covered even if a different QB played due to an injury.
- **Individual player logs**: two narrow scrapers reuse the same logs page for data not available through the category leaderboards — `extract_extra_points.py` for kickers (Extra Point attempts/makes have no dedicated leaderboard category) and `extract_defense_stats.py` for defensive positions (the "Tackles" leaderboard category currently returns no data on nfl.com).

## Known limitations

- **Tackles category unavailable**: nfl.com's own "Tackles" leaderboard page returns "No Stats Available" regardless of season or sort order. Tackle, sack, safety, and pass-defended totals are collected through a slower, per-player scraper instead.
- **Extra Points have no leaderboard**: attempts, makes, and blocks are collected per kicker rather than in bulk.
- **Punt Return columns unverified**: the `punt_return` table's column mapping was built assuming the same layout as Kickoff Returns; it has not yet been checked field by field against a real extraction.
- **Fumbles are approximate**: the pipeline can report fumbles forced and recovered by the defense, and fumbles committed while rushing or receiving, but not whether a given fumble was lost to the opposing team.
- **Mid-season trades**: nfl.com's category leaderboards report season totals per player without splitting them by team. A player traded mid-season will have his full-season total attributed to his current team once the roster scraper picks up the move.

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` (in the same folder as the scripts) and fill in the Supabase Postgres connection string (Supabase dashboard: Project Settings > Database > Connection string > URI).
3. Run the DDL and seed scripts against the Supabase database (SQL Editor in the Supabase dashboard, in order: `ddl_nfl_2026.sql`, then `seed_teams_positions.sql`).

Credentials are never hardcoded. Locally they are read from `.env` (excluded from version control by `.gitignore`); in GitHub Actions they are read from a repository secret (`SUPABASE_DB_URL`).

## Running the pipeline manually

```
cd sources
python extract_players_roster.py --output players_roster.csv
python extract_category_stats.py --year 2026 --output-dir stats_2026
python extract_qb_games.py --roster players_roster.csv --year 2026 --output games_2026.csv
python extract_extra_points.py --roster players_roster.csv --year 2026 --output extra_points_2026.csv
python extract_defense_stats.py --roster players_roster.csv --year 2026 --output defense_2026.csv
python transform_stats.py --year 2026 --roster players_roster.csv --stats-dir stats_2026 \
    --games games_2026.csv --extra-points extra_points_2026.csv --defense defense_2026.csv \
    --output-dir final_2026
python load_to_supabase.py --final-dir final_2026
```

`load_to_supabase.py` performs an upsert (`INSERT ... ON CONFLICT DO UPDATE`) keyed on each table's primary key, so running the full pipeline again in a later week updates existing rows with the latest season-to-date totals instead of duplicating or summing on top of them.

## Automation

`.github/workflows/weekly_extraction.yml` runs the entire pipeline above, in order, every Tuesday at 09:00 UTC, and can also be triggered manually from the Actions tab. Extracted and transformed CSVs are uploaded as a workflow artifact (kept for 14 days) on every run, successful or not, to help diagnose failures without having to reproduce them locally.
