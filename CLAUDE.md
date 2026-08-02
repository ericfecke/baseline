# Baseline — Claude Code Init

## What This Is

Interactive NBA efficiency dashboard. One dot per player/team on a scatter plot: Offensive Rating (x) vs. Defensive Rating (y, inverted so up-right = good). Filterable by team/conference/division/position/minutes/etc., URL-shareable, live through last night's games. The chart is the product — everything else is navigation around it.

Full specs live in Obsidian, not here:
`C:\Users\Eric\Desktop\Eric\obsidian\e_obisdian\Projects\NBA Stats Index`

Read `PRD.md`, `DATA.md`, `MODEL.md`, `STACK.md`, `ROADMAP.md` there before touching the corresponding area of the app. This file is the condensed, code-facing summary — when the two disagree, the Obsidian folder wins and this file needs updating in the same commit.

**Status:** Phase 0 (data-source validation). No app code yet.

---

## Non-negotiables (see PRD.md for full reasoning)

1. The scatter plot is the product — motion, hover feel, and craft are requirements, not polish.
2. Axes rescale to the current filter (relative framing), with a toggle to fixed league bounds.
3. "Live" = current through last night's games. No real-time.
4. Never call a third-party sports API from the browser. Ingest → our DB → build-time static JSON export → browser reads that. No runtime DB query from the client.
5. $0 budget. Flag clearly if a decision quietly requires spending money.
6. **Never plot a raw rate stat.** Every plotted value is shrunk toward a prior (empirical Bayes) and carries visible uncertainty (opacity/softness). A minutes threshold is a crude backstop only, never the mechanism. No statistical constant (`k`, priors) is hardcoded — estimate from real data, record the method and value in `MEMORY.md`.

---

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js (App Router) + TypeScript, strict mode |
| Styling / components | Tailwind + shadcn/ui (copied source, not a template fork) |
| Charts | visx for the hero scatter; Recharts for secondary charts |
| Motion | Framer Motion + d3 interpolators for scale transitions |
| Data fetching | TanStack Query |
| Database | Postgres — Supabase free tier (build-time store, not runtime dependency) |
| ORM | Drizzle |
| Ingest language | Python — owns `numpy`/`pandas`/`scipy`/`statsmodels` for the stats layer |
| Scheduling | GitHub Actions nightly (preferred — reads the `hoopR-nba-data` mirror, no IP block); Windows Task Scheduler only if Phase 0 falls back to local `nba_api` |
| State | URL query params as the only filter store — no Redux/Zustand |
| Hosting | Cloudflare Pages or Vercel free tier, static export |
| Validation | Zod (web) / Pydantic (ingest) at every external-data boundary |
| Testing | Vitest (unit — rating math, axis-domain logic) + Playwright (e2e) |

Full reasoning and the rejected alternatives: `STACK.md`.

---

## Pipeline (target shape — see ROADMAP.md for phase sequencing)

```
fetch → validate → normalize → resolve transactions → aggregate → MODEL (shrink) → QA → publish
```

- **fetch** — pull box scores from `hoopR-nba-data` (GitHub raw) or, on the fallback path, `nba_api` from a residential IP.
- **validate** — Pydantic models at the boundary. External data lies.
- **normalize** — canonical field names, numeric provider IDs (never name strings) as the join key.
- **resolve transactions** — reconstruct player team-stints (trade/signing/waiver/draft/two-way/call-up) from the game log; tag `acquisition_type`. See `DATA.md` §6a. Current-stint possessions become `n` for shrinkage downstream, not full-season combined.
- **aggregate** — possessions via the precise two-sided estimate (averages both teams' per-game estimates, ORB%-weighted; see `MODEL.md` §8a — the simpler `FGA + 0.44·FTA − OREB + TOV` formula was tried in Phase 0 and rejected, it reconciles against Basketball-Reference with a multi-point systematic offset); team ORtg/DRtg trivially from that; player ORtg/DRtg via Dean Oliver's formulas, validated against Basketball Reference until agreement is within rounding — achieved in Phase 0, see `MEMORY.md`.
- **MODEL** — empirical-Bayes shrinkage (`adjusted = w·observed + (1−w)·prior`, `w = n/(n+k)`), confidence intervals, robust (median/MAD, Mahalanobis) outlier flagging. Full design: `MODEL.md`.
- **QA** — integrity checks (impossible values, day-over-day jumps, team/player reconciliation) → 0–1 confidence score + flags in `ingest_runs`. Below threshold, reject the run and keep serving the last good snapshot.
- **publish** — write JSON snapshot → trigger static rebuild → redeploy. Nothing in the browser talks to Postgres directly.

Agent-per-stage, one module per stage, mirroring xml-auditor's `intake → reader → breakdown → qa → orchestrator` shape (github.com/ericfecke/xml-auditor). Thin routes, logic in modules — `app.py`/route handlers hold no business logic, same discipline as xml-auditor's `app.py`.

---

## Storage shape (starting point — see DATA.md §7 for the authoritative version)

```
teams(id, provider_id, name, abbr, conference, division, primary_color, secondary_color)
players(id, provider_id, name, team_id, position, jersey)
player_team_stints(id, player_id, team_id, season, start_date, end_date,
                    acquisition_type, boundary_source, gp, poss)
player_stats(id, player_id, season, as_of_date, stint_id, gp, min_total, min_per_game,
             off_rtg, def_rtg, net_rtg, pace, poss, usage, ts_pct, raw_payload,
             off_rtg_adj, def_rtg_adj, off_rtg_ci_lo, off_rtg_ci_hi, def_rtg_ci_lo, def_rtg_ci_hi,
             reliability, def_rtg_vs_team, outlier_score, sample_flag)
team_stats(id, team_id, season, as_of_date, gp, off_rtg, def_rtg, net_rtg, pace, raw_payload)
ingest_runs(id, started_at, finished_at, provider, status, rows_written, error)
```

Idempotent upsert on `(season, entity_id, as_of_date)`. Snapshot daily, don't overwrite — free historical trend charts later.

---

## Rules

- Docs in the Obsidian folder are the source of truth. When a decision changes, update the relevant `.md` there in the same commit as the code.
- No statistical constant hardcoded from memory or a blog post. Estimate from real data; record method + value in `MEMORY.md`.
- Backtest the model, don't inspect it. `MODEL.md` §8 — adjusted must beat raw on held-out prediction, tested against a past completed season while the current season is still in progress.
- Flag outliers, never delete them. `outlier_score` column; UI decides what to do with it.
- Fail loudly to the maintainer, quietly to users — on ingest/QA failure, keep serving the last good snapshot, never an empty chart.
- Two languages, one boundary: Python owns ingest, TypeScript owns the web app. They meet at the database and nowhere else — the ingest script doesn't know a website exists.
- Provider access behind one `StatsProvider` interface (`DATA.md` §4). The web app never knows which provider ran.
- Real data by end of Phase 1 — no mock-data phase that lingers.
- Every phase deploys. A preview URL per phase beats a big-bang launch.
- `prefers-reduced-motion` honored, non-negotiable, wherever motion is added.
- Dark mode from day one — retrofitting it later is miserable.
- No secrets in client components. Provider keys server-side only.
- See `MEMORY.md` for basketball domain knowledge, data quirks, and every hard-won fact discovered during ingest.

## Session hygiene

Start a fresh session per ROADMAP phase: "Read CLAUDE.md and ROADMAP.md, then execute Phase N." Don't start a phase until the previous one's acceptance criteria (stated in `ROADMAP.md`) pass.
