# Nishpaksh (निष्पक्ष) — handover for the next session

Hourly, bilingual (English + Hindi, `?lang=hi`) Indian news site that writes each story once, from
every outlet that covered it, and colours every sentence by how well it is supported. Owner: Aakash.

## How to work with the owner
- **Order: philosophy → structure → coding.** Discuss principles first, then a structure he approves,
  then build. In the coding phase make the implementation decisions yourself.
- **Be efficient.** No hours of testing or polling live runs. Test on stored real data
  (`tools/replay.py`), push, and check the next run's results in one look.
- He is truthful and disagreeable: give honest pushback, explain trade-offs plainly, no flattery.
- Overriding product rule: **minimise false positives.** Never show something as established
  (green) or false (red) when it is not; when unsure, be more cautious.

## Hard constraints
- Free tiers only. **No billing**, ever. Gemini free API (3 keys: `GEMINI_API_KEY`, `_2`, `_3`,
  separate projects; the owner decided this), Tavily free (1,000 credits/month, ≤31/day), Supabase free
  (stay well under 500 MB), GitHub Actions on a public repo.
- The pipeline's DB role (`nishpaksh_app`) cannot create or alter tables: schema changes go in
  `supabase/migrations/*.sql` and are applied with the Supabase MCP (`apply_migration`), with RLS policy
  `app_full_access` and grants for `nishpaksh_app`. The site reads `published` with the anon key.
- Destructive SQL through MCP needs the owner's approval (he may be away); prefer non-destructive steps.

## Decisions (do not undo without discussing)
- **Green (established):** 3+ independent outlets actually read (owner groups and wire copies merged,
  `config/ownership.yaml`), 2+ **independent origins** (`origins.py`: named sources; officials of one
  government = one origin; an outlet's own voice counts only as original reporting; unattributed /
  unnamed = pool, never counts), no denial, a checkable fact (not a characterisation), standing 6 h
  from when the rule was first met (before that: "developing", dotted grey).
- Grey = not yet cross-checked; amber = sources disagree; red with a FALSE tag = primary evidence
  (FIR, court record, official data, video) shows it false, two model families agreeing.
- **Interim publishing rule** while perspectives are unknown: 3+ independent read outlets and 2+
  origins. Perspectives emerge from agreement data (no hand labels of outlets).
- **Option B:** a page we could not read (headline/blurb only) is listed as "could not be read",
  never used for facts. Tavily reads blocked pages on budget.
- **Writing:** no outlet names in the text (source numbers carry them); claims pinned on whoever makes
  them; at most one hedge per paragraph; "after" is fine, cause words only if a statement has them;
  never invent a speaker; "allegedly" stays as long as the outlets say it. Suicide stories get the
  Tele-MANAS helpline note.
- **Only the writer produces prose (Oct 2026).** A new story is published only with a good essay
  (`narrative.essay_ok`: written by a Flash model, covers 60% of non-minor statements); otherwise it
  waits. Code-stitched pages and Flash-Lite essays are never kept (`compose._keepable`). A live
  essay is recoloured by code when only verdicts change (`narrative.recolour`); a rewrite is spent
  only on a material change (`needs_rewrite`). Rejected sentences are dropped, not patched; what the
  essay does not carry is listed under it ("Also reported"). A fallback headline never goes live.
  Writer failures per statement set are kept in `stories.analysis.writer_failures` (3 tries max).
- **Attribution like a newspaper:** name a speaker once, continue with "he said" in the same
  paragraph (validator honours the paragraph's speaker scope); "reports said" at most once per
  paragraph, as a leading "According to reports,"; a dispute states both versions and whose they
  are, never "other reports differ".
- **Red verdicts on hold** (`SETTINGS.model_verdicts = False`): no second model family on the free
  tier now Gemma fails most calls. Code verdicts still run. Tests keep the machinery on (conftest).
- **Headlines:** ≤12 words, one hammer-blow fact, people introduced by role, hook from the facts, no
  "reports say", no tacked-on "reportedly".
- **Threads:** a later development links to its earlier story (parent → daughter, many-to-many).
  Daughter opens with the new development + ≤2 background sentences + "Earlier in this story".
  Front page: one entry per thread (its latest development), ranked by importance; top 20, then
  "More stories". Filler is never published. A thread timeline page is a possible later step.
- **Grouping:** one embedding model only (`gemini-embedding-001`, chosen on 216 labelled real pairs);
  join only on high similarity to closest members and the story's fixed core, a model checks the
  middle band, stories holding separate events are split. When unsure, keep apart.
- **Scheduling:** Supabase `pg_cron` calls GitHub's workflow_dispatch at :05 every hour
  (`public.dispatch_pipeline()`, token in Vault `github_dispatch_token`); GitHub's own schedule is backup.

## Pipeline (nishpaksh/run.py)
ingest RSS → proactive search (`discover.py`: Google News decoded, Bing; Tavily fallback) →
retract headline-only reads → Tavily reads blocked pages → wire copies → grouping (`stories.py`) →
read articles (`extract.py`, Flash-Lite) → per story: match statements → consolidate
(`consolidate.py`: merge duplicates, contradictions, one spelling per name, who says what) →
perspectives → origins + fact/characterisation → verdicts (`verify.py`) → page (`compose.py`:
importance, threads, headline; `narrative.py`: the essay) → Hindi → retention → health checks in
`runs.stats.health`.

## Known quotas and facts learned from real data
- Google counts **each text in an embedding batch** as one request: ~1,000 texts/day per key.
- Flash models: 20/day each per key (writer/judge); Flash-Lite 500/day each per key; Gemma is
  unreliable on the free tier (overflow only). Grounded search ~20/day per model per key.
- Quotas reset at 00:00 Pacific = 12:30 IST. **Every tier is paced** over that day
  (`router.pace_fraction`: hours weighted 1.5 on 07:00-23:00 IST and 0.5 at night, plus 2 h
  slack; unused carries forward): unpaced, Flash-Lite ran
  out after ~15 runs and the site sat frozen from ~23:30 to 12:30 IST (Oct 4-5 2026). Dispatch the
  hourly workflow with reason `backfill` for a deliberate unpaced catch-up.
- `light` = analysis (decides colours); `page` = headline, importance, threads, Hindi (same models,
  nothing kept back, so page building is never the step starved). Before moving a light/page task
  to another model, run `tools/modelcmp` (workflow `modelcmp`).
- Health flags a tier that cannot pay for one call (embed < one 25-text batch) and grouping that
  stalls for 3 runs while articles keep coming in.
- Run logs are readable via the `runs` table (stats + health). A crashing run writes its traceback to
  `diagnostics` (kind 'crash'). `gh run list/view` shows run status, but job log downloads were refused
  (403) from the sandbox; tools write to the `diagnostics` table instead.

## Tools
- `python -m nishpaksh.tools.replay --stories 10192,10254` (workflow `replay`, dispatch via
  Supabase `net.http_post` with the Vault token): re-run writing stages on stored stories, site untouched.
- `python -m nishpaksh.tools.modelcmp --variants flash,flash2,gemma`: same stories analysed from
  scratch per model; per-statement colour confusion vs Flash-Lite's own run-to-run noise.
- `python -m nishpaksh.tools.probe --parts search,fetch,grouping,embedcmp`: measurements on real data.
- Tests: `python -m pytest -q` (fake backend in `tests/fixtures.py`; add fakes for any new prompt).

## Open items
- Green rule may be too strict (own-voice reporting rarely counts as an origin); revisit with data.
- Thread timeline page (later). Weak fallback headlines when the model fails twice.
- Perspective clusters flipped because they were noise (Oct 5 2026: 32 sources, 21% of pairs
  observed, 1,020 of 1,164 pairs seen once, agreement centred on 0, silhouette 0.04-0.07, resample
  ARI 0.02-0.68). Now shown only if stable under resampling (`global_min_stability`), and live pages
  are relabelled when clusters change.
- **Perspective units and evidence (owner, Oct 5 2026):** clustered over OUTLETS; each article is
  still scored on its own and departs from its outlet's perspective only on strong evidence
  (margin 0.5, 3+ items; shown with †); an author with 3 of their last 5 assessed articles departing
  becomes their own unit (`refresh_units`); outlets whose articles depart ≥30% are flagged in health.
  Evidence, strongest first: stance on contested facts (1), whose named voices are carried (0.5),
  loaded words for the same fact (0.5), omission (0.15). Never anything about the outlet itself.
  At outlet level on the old omission-heavy signal: silhouette 0.07, resample ARI ~0.35 (still noise,
  gated off); the voices/words signals are new, judge them after a few days of data.
- Writer failures on big stories: the reason is now split ("empty sentence" / "no valid ids" / "too
  long", plus reply-level `failure`); read `stories.analysis.writer_failures` before shortening input.
- Fallback sentences append `when_text` that can be relative to another event ("on Thursday, a day
  before his arrest" on the arrest itself); prefer the absolute date in fallbacks.

## Fixed at the root (Oct 2026), keep the guards
- Name consolidation maps only spelling variants (`consolidate.is_spelling_variant`); aliases were
  rewriting "X, also known as Y" into "Y, also known as Y". Old bad mappings are undone on the next run.
- `when_text` is extracted in English; `narrative.english_when` converts any stored Devanagari.
- Writer validator rejects Hindi in English text, and reported speech ("A said that B claimed X")
  turned into a fact or pinned on A.
- `compose.tidy` removes "X (X)" duplicates.
- The headline model gets statements dated and newest first (`compose._newest_first`); sorted by support,
  old background outranked the new development and got tied to it with "after".
