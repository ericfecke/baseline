# Baseline — Domain Memory

Basketball domain knowledge and every data quirk discovered during ingest. Filled in as Phase 0 and later phases run — this file starts mostly skeleton, not because nothing is known yet, but because most of what's known so far is *decided methodology* (lives in `MODEL.md`/`DATA.md`), not yet *observed data facts*.

---

## Rating formulas (decided methodology — see MODEL.md §8a for full derivation)

**Possessions (team level) — precise two-sided estimate, corrected during Phase 0 (2026-08-02):**
```
possessions ≈ 0.5 × [ (Tm_FGA + 0.4·Tm_FTA − 1.07·(Tm_ORB/(Tm_ORB+Opp_DRB))·(Tm_FGA−Tm_FG) + Tm_TOV)
                     + (Opp_FGA + 0.4·Opp_FTA − 1.07·(Opp_ORB/(Opp_ORB+Tm_DRB))·(Opp_FGA−Opp_FG) + Opp_TOV) ]
team ORtg   = 100 × points / possessions
team DRtg   = 100 × opponent_points / opponent_possessions
```
The originally-specified simplified formula (`FGA + 0.44·FTA − OREB + TOV`) was tried first — it's internally consistent (league ORtg == league DRtg exactly, confirmed both ways) but reconciled against Basketball-Reference with a systematic ~2.5–3.5 pt offset on both ORtg and DRtg (net rating looked fine, which is why the bug wasn't visible from net rating alone — the offset cancels in the subtraction). Switched to the precise version above; gap closed to ~0.2 pts on every team. DATA.md §2b and MODEL.md §8a updated to match.

**Player level:** Dean Oliver's individual offensive/defensive rating formulas (*Basketball on Paper*) — scoring possessions, floor percentage, and team context for offense; stops plus team defensive totals for defense. **Implemented and validated in Phase 0** (`ingest/phase0_validate.py`) — computed on season-aggregated totals (matching how Basketball-Reference derives its published table, not summed per-game ratings). Reconciliation tolerance achieved: within ~0.5 rating points (both ORtg and DRtg) across 6 spot-checked players once the precise possession formula was used — see log below.

---

## Data source — what Phase 0 finds (fill in as discovered)

### hoopR-nba-data (preferred path) — confirmed 2026-08-02

- **Player box score:** `nba/player_box/parquet/player_box_{season}.parquet` — one file per season, e.g. `player_box_2026.parquet` for the 2025-26 season. Fetch via GitHub raw: `https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-data/main/nba/player_box/parquet/player_box_2026.parquet`. Parquet format — read with `pandas.read_parquet` (needs `pyarrow`).
- **Team box score:** same pattern, `nba/team_box/parquet/team_box_{season}.parquet`.
- **Season file-year convention:** file year = the year the season *ends* (matches NBA convention: 2025-26 season → `2026`).
- **Also present** (not needed for v1, noted for later): `nba/pbp` (play-by-play), `nba/rosters` + `nba/game_rosters` (roster/transaction data — worth checking first as the boundary source for `player_team_stints` in DATA.md §6a, before falling back to game-log inference), `nba/schedules`, `nba/standings`, `nba/draft`, `nba/betting_lines`.
- **Update cadence:** commits to `player_box_2026.parquet` on 2026-07-12, 07-13, 07-14 (near-daily) — during NBA Summer League, which runs into mid-July. Last commit 2026-07-14; no commits since because there are no games in the current offseason window (checked 2026-08-02). Cadence tracks actual game activity, as expected — re-verify daily cadence once the regular season is underway.
- **License — resolved, better than DATA.md assumed.** GitHub's UI reports license `"Other"/NOASSERTION` because the top-level `LICENSE` file is just an R-package boilerplate stub (`usethis::use_ccby_license()` template — copyright year/holder only, no actual terms). The **real** terms are in `LICENSE.md`: **CC BY 4.0** — share and adapt freely, **commercial use explicitly permitted**, attribution required. This removes the license uncertainty DATA.md flagged as an open Phase 0 checklist item. (Baseline is non-commercial anyway per the STACK.md decision, so this was never a blocker — but it's now a non-issue either way, and CC BY is *more* permissive than the NBA.com ToS gray area DATA.md spent time on.)
- **Current season present:** `player_box_2026.parquet` and `team_box_2026.parquet` both exist and are current through the last Summer League games in the file. Confirms the mirror repo carries current-season data as DATA.md hoped.
- Repo pushed at 2026-08-02 (today) — actively maintained.

### Field name quirks and dtypes (confirmed 2026-08-07, Phase 1)

- **`athlete_id` is `float64`, not an integer type**, because the column contains nulls (33 rows in 2025-26 regular season). Coerce to `int` after dropping nulls — provider IDs are the only join key we use (DATA.md §6), so a float ID is a bug waiting to happen. The 33 null-`athlete_id` rows carry a name too, so they're not team-total rows; they're just unattributable and get dropped with a counted reason.
- **Missing numerics are `NaN`, not `None`.** Matters more than it sounds: `NaN` *is* a float, so it satisfies a `Optional[float]` annotation and then fails every `ge=0` bound, producing a wall of confusing validation errors for what is really "this player didn't play." Convert `NaN → None` at the boundary (done in `_StrictRow._nan_to_none`).
- **A DNP player has nulls across the entire box line** (~5,541 rows), not zeros. `did_not_play=True` always implies null minutes; the converse isn't quite true (35 rows have `did_not_play=False` with null minutes).
- **`team_color` / `team_alternate_color` are hex strings without the `#`** (e.g. `'1d428a'`), and are null on 73 rows. Needed for PRD §3's team-color dot encoding — prepend `#` and have a fallback.

### `gp` (games played) — use `minutes is not None`, never `active`

This resolves the Gobert discrepancy flagged in Phase 0. Rudy Gobert has **79** rows in the 2025-26 regular season but only **76** with a box line, and Basketball-Reference reports **G=76**. So games-played counts rows where `minutes` is not null.

**Do not use the `active` column for this.** It tracks roster status, not participation — Gobert shows `active=True` on only **14** of his 76 played games. Across the dataset `active` splits 12,291 True / 14,354 False among rows that *did* have minutes, so it's uncorrelated with playing. Easy trap, badly wrong answer.

### The three turnover columns, and a real upstream data error

`turnovers` = player-attributable, `team_turnovers` = charged to the team (shot clock, backcourt), `total_turnovers` = both. **The possessions formula needs `total_turnovers`** (a team turnover ends a possession too).

Two things learned digging into this:

1. **`team_turnovers` is a derived residual**, not an observed stat — it equals `total_turnovers − Σ(player turnovers)`, which holds in 2,458/2,462 rows. Consequence: **it can be negative** (range observed: −16 to +5). Don't bound it at ≥ 0 in validation, or 4 bad upstream rows kill an entire nightly run.
2. **`total_turnovers` is sometimes under-reported — a genuine upstream error affecting our ratings.** In 4 rows it came in *below* the player-attributable `turnovers`, which is impossible. Worst case is game **401810469** (Clippers/Bulls), which reports `total_turnovers = 0` for *both* teams while their players individually recorded 16 and 12. Left alone, that game's possession estimate is short by ~16/~12, inflating both teams' ORtg for it.
   **Handling:** `TeamBoxRow.possession_turnovers` returns `max(total_turnovers, turnovers)` — repairs the floor without inventing data, since we never claim more turnovers than one of the two sources reports. `turnovers_repaired` exposes when it fired so QA can flag it rather than repairing silently. Affects 4/2462 rows (0.16%), so season-level impact is small but real.

**Validator design rule that came out of this:** quirks we've characterised get filtered/repaired *and counted*; anything uncharacterised fails the run loudly. The turnover *identity* (`total == turnovers + team_turnovers`) is enforced strictly, because if that ever breaks the provider changed a column's meaning and the possessions formula needs re-deriving.

### hoopR has no transactions feed — what `acquisition_type` can and can't say (2026-08-16)

Investigated all four candidate hoopR datasets to label *why* a player changed teams. Conclusion: **the mechanism of a move is not derivable from any free source currently in play.**

| Dataset | Rows (2025-26) | Verdict |
|---|---|---|
| `nba/rosters` | 537 | **Useless for this.** A *current snapshot*: one row per athlete, `status_name` = "Active" for all 537, no dates, no history, nobody on two teams. |
| `nba/game_rosters` | 34,883 | **Useless for this.** The promising-looking `reason` column is injury/DNP text — "COACH'S DECISION" (33,650), "ILLNESS", "SUSPENDED BY LEAGUE". Not movement. |
| `nba/player_core` | 591 | **Useful.** Carries `draft_year` / `draft_round` / `draft_selection` for 434 players, plus bio fields (height, weight, age, college) worth having for the Phase 5 detail panel. |
| `nba/draft` | — | Redundant; `player_core` already carries the draft fields we need. |

**What we emit, and why only this:**

- `rookie_debut` (55) — from `player_core.draft_year == season − 1`. The draft precedes the season, so 2025-26 (`season = 2026`) has the 2025 class. **Verified against the known class:** Flagg #1, Harper #2, Edgecombe #3, Knueppel #4, Bailey #5 all correct. **Do not use `experience_years`** — it reports **1** for first-year players (Flagg: `experience_years = 1.0`) while 22 others show 0, so it doesn't identify rookies at all.
- `season_start` (527) — first stint, not a current-class rookie.
- `team_change` (79) — moved between NBA teams mid-season. We know *that*, never *how*.

**Two labels deliberately rejected:**

1. **`trade` / `signing` / `waiver` / `g_league_callup`** — would require inventing a mechanism. A fan reading "Traded to Phoenix" would believe it.
2. **`mid_season_debut`** (signed later vs. on the opening roster) — separating these needs a cutoff on how late a first appearance came, and the distribution has **no natural break**: 332 players debut on their team's opening night, then a smooth decay out to 173 days. Any threshold would be invented. `start_date` is published on every stint, so the UI can state "first appeared 12 February" as fact instead.

**Cross-validation that the stint splitting is right:** CJ McCollum comes out as two stints of 35 and 41 games; Basketball-Reference lists him as 2TM with exactly 35 (WAS) and 41 (ATL). There's a test pinning this, which also confirms stint possessions are the post-move sample — the correct `n` for Phase 2 shrinkage.

### The NBA Cup Championship must be excluded (found 2026-08-07)

ESPN/hoopR tags the **NBA Cup (In-Season Tournament) Championship game** as `season_type == 2`, but it does **not** count toward regular-season statistics — officially, or in Basketball-Reference. Left in, the two finalists show **83** games instead of 82, and `gp` and `pace` are wrong by a game for every player on both rosters.

Found by chasing a +1 `gp` discrepancy on Luke Kornet (ours 69, BRef 68) and Victor Wembanyama (65 vs 64) — both San Antonio, which was one of the two teams showing 83 games. 2025-26's game is `401809839`, 2025-12-16, NYK 124 – SAS 113 at T-Mobile Arena.

**Detect it from schedule metadata, never from the date or game counts.** `nba/schedules/parquet/nba_schedule_{season}.parquet` carries per-game type fields. Within `season_type == 2` the taxonomy is exactly:

| `type_id` | `type_abbreviation` | Count (2025-26) | Counts toward regular season? |
|---|---|---|---|
| 1 | `STD` | 1,234 | **Yes** |
| 4 | `ALLSTAR` | 4 | No |
| 39 | `CC` | 1 | **No** — the Cup final |

So the rule is `type_id in {4, 39}`. A date-based or "which team has 83 games" heuristic would also be wrong mid-season, when no team has hit 83 yet but the game is already in the data.

**Do not over-correct.** Every *other* NBA Cup game counts and is typed `STD` — group play (60 games), quarterfinals (4), semifinals (2). Only the final is excluded. `notes_headline` distinguishes them (`'NBA Cup - Group Play'`, `'NBA Cup Championship'`, etc.) but `type_id` is the reliable key. There's a test guarding against dropping the group-play games.

After this fix, all 30 teams show 82 games and all 10 spot-checked players match BRef exactly on `gp`, with ratings within 1.0.

**Related trap:** `neutral_site == True` is *not* a usable proxy — 6 season_type-2 games are at neutral sites (London, Berlin, Mexico City, plus the Cup semis and final), and the international games absolutely do count.

### nba_api (fallback path, if 0a fails)

_Not yet attempted — only pursue if hoopR-nba-data proves unusable._

---

## Statistical constants (estimate from data — never hardcode)

| Constant | Value | Method | Date estimated | Season(s) used |
|---|---|---|---|---|
| `k` (stabilization constant, ORtg) | _TBD_ | _TBD — split-half + variance decomposition, MODEL.md §2_ | | |
| `k` (stabilization constant, DRtg) | _TBD_ | | | |

Re-estimate each season. If split-half and variance-decomposition methods disagree badly, something upstream is wrong — investigate before picking one.

---

## Reconciliation log

Track every time computed ratings were checked against Basketball Reference. This is the evidence that the pipeline produces trustworthy numbers.

Source: `basketball-reference.com/leagues/NBA_2026_per_poss.html` (individual ORtg/DRtg) and `.../NBA_2026_ratings.html` (team, unadjusted columns — no strength-of-schedule adjustment attempted, that's v2 scope). Checked via one-time manual browser navigation, not a scraper — matches DATA.md §8's "manual spot-check only" rule.

**Team ratings (2025-26, full season, all 30 teams checked, 10 shown):**

| Team | Our ORtg/DRtg | BRef ORtg/DRtg | Delta |
|---|---|---|---|
| OKC Thunder | 118.92 / 107.78 | 118.94 / 107.89 | ~0.1 |
| San Antonio Spurs | 119.66 / 111.60 | 119.68 / 111.41 | ~0.1–0.2 |
| Detroit Pistons | 117.93 / 109.76 | 118.05 / 109.81 | ~0.1 |
| Boston Celtics | 120.80 / 112.71 | 120.82 / 112.67 | ~0.02–0.04 |
| New York Knicks | 120.03 / 113.45 | 119.87 / 113.40 | ~0.1–0.2 |
| Denver Nuggets | 122.61 / 117.44 | 122.63 / 117.46 | ~0.02 |
| Houston Rockets | 118.61 / 113.24 | 118.69 / 113.30 | ~0.1 |
| Cleveland Cavaliers | 119.22 / 115.12 | 119.26 / 115.20 | ~0.1 |
| Minnesota Timberwolves | 116.79 / 113.47 | 116.76 / 113.68 | ~0.03–0.2 |
| Toronto Raptors | 115.92 / 113.06 | 115.83 / 113.25 | ~0.1–0.2 |

League-wide: our ORtg = our DRtg = 115.81 exactly (internal consistency check, holds by construction).

**Player ratings (2025-26, season totals, 6 spot-checked):**

| Player | Our ORtg/DRtg | BRef ORtg/DRtg | Delta |
|---|---|---|---|
| Luke Kornet (SAS) | 154.3 / 111.1 | 154 / 111 | ~0.1–0.3 |
| Ryan Kalkbrenner (CHO) | 142.4 / 112.4 | 142 / 112 | ~0.4 |
| Neemias Queta (BOS) | 138.4 / 108.4 | 138 / 108 | ~0.4 |
| Nikola Jokić (DEN) | 133.6 / 111.6 | 134 / 111 | ~0.4–0.6 |
| Shai Gilgeous-Alexander (OKC) | 133.3 / 108.8 | 133 / 109 | ~0.2–0.3 |
| Rudy Gobert (MIN) | 131.8 / 110.1 | 132 / 110 | ~0.1–0.2 |

**Verdict: within rounding error** (largest observed delta ~0.6 on a 100+ scale) once the precise possession formula was used. Before that fix, ORtg matched almost exactly (Oliver's offensive formula doesn't lean on team-level possessions as heavily) but DRtg was systematically ~1–2 pts low — same root cause as the team-level offset, since individual DRtg's dominant term is the team's own defensive rating.

**Known minor gap, not yet resolved:** Rudy Gobert's game count came out 79 (ours) vs. 76 (BRef), despite very close total minutes (2378 vs 2380). Likely our `gp = game_id.nunique()` counts games where the player has a box-score row with 0 minutes (DNP-suspended/inactive-but-listed), which BRef's `G` column excludes. Doesn't affect the rating math (minutes and possession-weighted stats matched closely regardless), but Phase 1's ingest should filter `gp` to games with `minutes > 0` (or use the `did_not_play`/`active` columns already present in hoopR's player_box) to match convention before this becomes a displayed stat.

---

## Data quality traps encountered in practice

_Append here as they're found. Seed list from DATA.md §6 — confirm or refute each against real data:_

- **All-Star Weekend games are NOT distinguishable by `season_type` alone.** hoopR's `player_box`/`team_box` tags Team Stars / Team Stripes / World (All-Star Game, Rising Stars) as `season_type == 2`, identical to real regular-season games. Confirmed 2025-26: 3 exhibition "teams" appeared alongside the real 30, each with 2-3 games and pace ~33 (vs. ~100-106 real) — would badly corrupt team ratings and inflate All-Star selections' possession counts if not filtered. **Fix:** the 30 real ESPN franchise `team_id`s are exactly `1-30`; exhibition teams use large synthetic IDs (observed: 111386, 132374, 132375). Filter `team_id.isin(range(1,31))` on both player_box and team_box before aggregating. This must carry into the real Phase 1 ingest pipeline, not just this validation script.
- **In-season tournament (NBA Cup) final adds an extra game for its two participants.** Observed 2025-26: San Antonio Spurs and New York Knicks show `gp=83` where every other team shows `82`. The Cup championship game counts as a real regular-season game for those two teams only. Not a bug — just don't assume every team has an identical game count when validating row counts.
- **`turnovers` vs. `team_turnovers` vs. `total_turnovers` (team_box).** `turnovers` = turnovers individually attributable to a player (sums to the player-level figure); `team_turnovers` = turnovers charged to the team itself (shot-clock/backcourt violations etc., not attributable to one player); `total_turnovers` = the sum of both. **The possessions formula (`FGA + 0.44·FTA − OREB + TOV`) needs `total_turnovers`** — a team turnover still ends a possession. Using bare `turnovers` would systematically undercount possessions. Confirmed by computing league-wide ORtg vs. DRtg with `total_turnovers` — they matched to 2 decimal places (112.75 = 112.75) as they must by construction; this only held with the full total.
- **Player-level `turnovers` (not `total_turnovers`) is the correct input to Oliver's individual formulas** — team-charged turnovers aren't attributable to a specific player's box line, so summing individual `turnovers` per player is right; don't apply the team-level `total_turnovers` fix to the player-level table.
- **Traded players / `acquisition_type` — resolved as far as the data allows (2026-08-16).** See the dedicated section below.
- Two-way / G-League players — **not identifiable.** `rosters.status_name` is "Active" for all 537 players, so there is no two-way or G-League flag anywhere in hoopR. They simply appear as ordinary small-sample players, which the Phase 2 shrinkage handles correctly anyway.
- Defensive rating team-context distortion — magnitude observed once shrinkage (Phase 2) is built.
- Possessions vs. minutes-only providers — not applicable; hoopR gives full box scores, so the `minutes × team_pace / 48` fallback estimate was never needed for this provider.
- Offseason behavior — confirmed: no new commits to `player_box_2026.parquet` since 2026-07-14 (last Summer League game), during the current offseason check on 2026-08-02. Matches DATA.md's expected behavior.

---

## CI environment gotchas (Phase 0 GitHub Actions debugging, 2026-08-02)

Getting the Phase 0 validation workflow to pass took 4 runs and 3 distinct real bugs, none of which showed up locally (Windows, Python 3.8). That workflow and `ingest/phase0_validate.py` were removed in Phase 1 — superseded by `.github/workflows/nightly-ingest.yml` and `ingest/ratings.py`, with the Basketball-Reference reconciliation now asserted in `tests/test_ingest.py` rather than eyeballed from printed output. The lessons still apply to the nightly workflow, which runs on the same runner image:

1. **`curl` without `-f` doesn't fail on HTTP errors.** `curl -sL -o file url` exits 0 even on a 404/error response — it just writes the error body to the file. Always use `curl -fsSL` for anything downstream expects to be real data, and per DATA.md's "validate every response at the boundary" rule, added a minimum-file-size sanity check after the fetch step too. This wasn't actually the cause of the failures below, but it was a real, silent gap — fixed regardless.
2. **GitHub's Ubuntu runners don't reliably give Python a UTF-8 stdout.** A plain `print()` with an em-dash raised `UnicodeEncodeError` and crashed the script. Reproduced locally by forcing `PYTHONIOENCODING=ascii`. Fix: `sys.stdout.reconfigure(encoding="utf-8")` at the top of any script that will run in CI and prints non-ASCII text (player names with diacritics are a realistic future trigger, even though hoopR's `athlete_display_name` field turned out to already be ASCII-normalized — "Jokic" not "Jokić").
3. **The actual root cause: numpy/pandas ABI mismatch.** `requirements.txt` pinned `pandas==2.0.3` but left `numpy` unpinned. Locally, pip resolved `numpy==1.24.4` (last numpy release with Python 3.8 wheels — compatible with pandas 2.0.3, built against numpy 1.x ABI). On the CI runner (Python 3.11.15), pip resolved numpy 2.x (wheels available for 3.11), which broke ABI compatibility with the pandas 2.0.3 wheel: `ValueError: numpy.dtype size changed... Expected 96 from C header, got 88 from PyObject`. **Lesson: pin numpy explicitly whenever pandas is pinned** — don't rely on pip's resolver picking a compatible pair, since the "compatible" version differs by Python version and wheel availability, and local dev on an older Python can mask a break that only shows up on CI's newer Python.

**Debugging note on process:** the first two fixes were real, verified bugs (confirmed by local repro before pushing), but neither was *the* blocker — the numpy ABI issue was underneath both, and guessing blind burned two CI runs. Getting the actual traceback from the Actions UI (anonymous API access can list runs/jobs but can't fetch job logs — 403) immediately identified the real cause. **For future CI debugging: ask for the actual log text after one blind attempt, don't keep guessing.**

**Resolved 2026-08-07 — local toolchain now matches CI.** Installed Python 3.11.9 via `winget install Python.Python.3.11` and rebuilt `.venv` against it; the pinned deps (numpy 1.24.4 / pandas 2.0.3 / pyarrow 17.0.0) all resolve cleanly on 3.11, independently confirming the pin fix. Re-ran `ingest/phase0_validate.py` and diffed both derived CSVs against the Python 3.8 output: **byte-identical**, so the interpreter change moved no numbers. Two notes for future installs: python.org only ships Windows binary installers for 3.11 up to **3.11.9** (3.11.10+ are security-only, source-tarball-only), so 3.11.9 is the newest installable 3.11 — patch-level gap vs. CI's 3.11.15 is irrelevant (same `cp311` ABI and wheel tags). And **do not jump to 3.12+** while numpy is pinned at 1.24.4 — that version has no 3.12 wheels (numpy added 3.12 support in 1.26), so it would recreate this exact bug class.

## Cross-project notes

- Sibling reference project: `github.com/ericfecke/xml-auditor` — CLAUDE.md/MEMORY.md split and agent-per-stage pipeline pattern reused here. Not otherwise related (job-feed auditing vs. NBA stats).
