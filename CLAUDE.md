# Nishpaksh (निष्पक्ष) — handover for the next session

Hourly, bilingual (English + Hindi, `?lang=hi`) Indian news site that writes each story once, from
every outlet that covered it, and colours every sentence by how well it is supported. Owner: Aakash.

## How to work with the owner
- **Order: philosophy → structure → coding.** Discuss principles first, then a structure he approves,
  then build. In the coding phase make the implementation decisions yourself.
- **Be efficient.** No hours of testing or polling live runs. Test on stored real data
  (`tools/replay.py`), push, and check the next run's results in one look.
- He is truthful and disagreeable: give honest pushback, explain trade-offs plainly, no flattery.
- **The models are simple (owner, Oct 7 2026).** Old, small, free-tier models: design every fix for them.
  Code does the work wherever it can; a model gets one small, concrete question (a fixed choice like
  same/different, short batches, worked examples including the traps, "if unsure: the safe answer");
  a model answer that could make something green or merge facts is asked twice (order swapped) and
  checked by code; a writing rule is enforced by code, never left to the prompt alone.
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
  **Purple = one outlet only (owner, Oct 7 2026):** a statement only one independent outlet reports
  (and not disputed or false) goes INTO the article, shown purple ("one outlet only": an exclusive,
  or a mistake); the colour, not words, says so (see "One author's voice"). `narrative.shade`; the writer and the revision pass must
  use every statement, one-outlet lines included (they were "minor" and optional before).
- **Interim publishing rule** while perspectives are unknown: 3+ independent read outlets and 2+
  origins. Perspectives emerge from agreement data (no hand labels of outlets).
- **Option B:** a page we could not read (headline/blurb only) is listed as "could not be read",
  never used for facts. Tavily reads blocked pages on budget.
- **One author's voice; the colour carries the support (owner, Oct 7 2026, option A).** No "according
  to reports", "reportedly", "reports said", "one report said" anywhere: the article reads as one author
  writing for a reader, and the colour key sits between the headline and the article (site). Kept in
  words only what colour cannot carry: a named speaker's claim ("police said"), "allegedly" where the
  outlets use it, and a dispute's two versions and whose. Code removes hedge words
  (`narrative._one_hedge`, not in disputes) and no longer adds a paragraph hedge. House style by code
  (`style.py`, after the spelling pass): a person's full name and title once, then the surname (not
  when two names share it; only with evidence it is a person: a title or role before it, a speech verb
  after it, or a speaker), and "He said ... He added ..." runs become "..., he said."
- **Writing:** no outlet names in the text (source numbers carry them); claims pinned on whoever makes
  them; no hedge words (above); "after" is fine, cause words only if a statement has them;
  never invent a speaker; "allegedly" stays as long as the outlets say it. Suicide stories get the
  Tele-MANAS helpline note.
- **Only the writer produces prose (Oct 2026).** A new story is published only with a good essay
  (`narrative.essay_ok`); otherwise it waits. Writer order (models.yaml): every Flash model, best
  first, then **3.5 Flash-Lite as the last resort** (owner, Oct 6 2026, after Flash refused on all keys
  for a day; it keeps 300/day per key for reading). Code-stitched pages and older Flash-Lite essays
  are never kept (`compose._keepable`, `WRITER_LITE_OK`). Rejected sentences are dropped, not
  patched. A fallback headline never goes live. Writer failures per statement set are kept in
  `stories.analysis.writer_failures` (3 tries max).
- **Editions: written once, like a newspaper (owner, Oct 5 2026; `editions.py`).**
  - An article is written when coverage has SETTLED: publishing rule met, then no new independent
    outlet for 3 h, or 8 h after the rule was first met (`analysis.edition.met_at`). On Oct 1-5 data a
    3 h wait saw 86% of a story's outlets. A story whose newest source is >36 h old is not written.
  - A published article is CLOSED: text, headline, statements, sections, perspectives never change;
    no article joins it, nothing is read or analysed for it, no model call is spent on it. Only its
    colours mature by code (`editions.mature`: the 6-hour clock on the evidence it was written with).
    The parent is never corrected: a follow-up carries any dispute.
  - Later reports of a published event go to a CANDIDATE story (`stories.py` redirects joins;
    `analysis.edition.follows`). It is read like any story and published only as a follow-up when,
    against its parent (`follow_up_ok`, one Flash-Lite call per statement set): 4+ new non-minor core
    statements (or half the parent's) carried by 3+ independent outlets, or a MAJOR development
    (arrest, FIR/charges, court order/verdict/bail, deaths, resignation/sacking, official decision,
    result) carried by 3+. On the parent's IST date: only a major development carried by 5+.
  - After 3 days the article moves to the `archive` branch (`pagearchive.py`, step in hourly.yml:
    `pages/<id>.json.gz`, `index/<YYYY-MM>.jsonl`) and is deleted from Supabase only after the push.
    The site and follow-ups read archived parents from the branch (raw.githubusercontent.com).
- **Writing desk and preparation queue (owner, Oct 6 2026).** Target: at least one article an hour,
  at most two. The writer is its own job (`desk.py`, workflow `writer.yml`, dispatched at :45 UTC = :15 IST by
  pg_cron `public.dispatch_desk()`, same Vault token; GitHub schedule :55 as backup; pg_cron is UTC, the
  pipeline starts :05 UTC = :35 IST): it writes the
  settled stories most important first until the clock hour has `desk_per_hour` (2) articles; a try
  (`desk_tries`, 5) counts only when the writer is asked; stories turned away before that (a follow-up
  refused, no headline) are counted in `skipped` and the desk moves on (at most 25 looked at), and a
  refused follow-up is not looked at again until a new outlet joins (Oct 7 2026: three refused
  candidates used all five tries two runs in a row and nothing was written). A failed headline no
  longer stops a story before writing: the article is written, the headline written from its lead, and
  if that fails too the finished article waits as a kept draft (outcome "headline failed"). Models that
  refused on every key in the previous desk run (within 75 min, `router.dropped`, logged as `dropped`)
  are skipped for one run (`desk.refused_last_run`, logged as `skipping`), so every other run tries
  Flash again (Oct 7 2026: ~15 refused Flash calls per run before Flash-Lite wrote); it logs
  to `diagnostics` (kind 'desk'), never `runs` (the gate spaces pipeline runs by `runs`). The pipeline
  (:05) only prepares: every story with 3+ independent sources is rated from its HEADLINES
  (`priority.rank_new`, one Flash-Lite call per 20 stories, importance rubric 1-5 + filler), and only
  the best `prep_queue` (16) are read, analysed, Tavily-read and searched first (`priority.queue`);
  the rest wait as headlines and enter when they rank higher (re-rated when coverage grows by 2+).
  Oct 6 before this: 458 articles read in 95 stories in 8 h for 8 published (~160 Flash-Lite calls
  per article). 3.5 Flash-Lite is the writer's first claim: reading stops at 250 left, analysis at 120,
  page at 80 (models.yaml `keep`); writer calls try up to 16 times so they reach it. Quota usage is
  saved as increments (`Store.quota_add`), since both jobs share the keys.
- **Story layers (Oct 5 2026, owner):** a story has its own event (core) and CONTEXT: background,
  related events (a separate event the reports connect to this one: written as separate, never
  blended), explanation, reactions, what next. Extraction records context (`claims.rel.context`),
  consolidation can re-label statements (`analysis.roles`). Headline, timeline and essay_ok use the
  core only; context is written after it and coloured like everything else.
- **A contradiction names its values (Oct 5 2026):** "contradict" only with two values that cannot both
  be true ("40 vs 50", "Friday vs Saturday", "arrested vs not arrested"), checked in code
  (`match.real_difference`); the quick pairwise check trusts only numbers, names, dates and negation
  (`typed_difference`); consolidation re-judges every earlier contradiction with the whole story in
  view and takes back what it does not confirm. The same event told from two sides is "same".
  **Different is not incompatible (Oct 6 2026):** every proposed contradiction (model, earlier mark,
  or "same" statements with different numbers) must pass one separately asked question, "can both be
  true?" (`match.check_conflicts`): kept only for "cannot both be true" (same question, different
  answers). Two steps of one thing (signed / took effect), a target vs a pledge of the same figures,
  one statement adding a number: not disputes. "Unsure": not amber, but neither statement can be
  established (`analysis.doubtful_conflicts`). Answers cached in `analysis.conflict_checks`. The
  writer puts a real dispute with its subject, never in a closing "accounts differ" paragraph.
- **Frames: statements compared field by field (owner, Oct 6 2026; `frames.py`).** Reading gives every
  statement who / action (base verb) / what / value / where / negated; words are reduced to roots
  (Snowball stemmer), numbers read as numbers (crore, million, "at least", "about"). Code compares:
  same slot (who, action, what) + agreeing values = one fact (merged); same slot + incompatible values
  or negation = contradiction (the ONLY way to one between statements with frames); a missing value =
  compatible (never a dispute, wording decides merging); same slot at different times = unsure;
  anything else = different. Frames now only PROPOSE: they no longer decide or veto whether two lines
  are the same fact (see "One structure for the same fact"). A day-precision date covers the whole day (`_times_apart`). Dates are read as dates ("6 December 1986" = "1986-12-06"; a year agrees
  with a full date in it), and a dispute needs the same object: a capitalised word in one name the other
  lacks makes two things ("Param Vishisht Seva Medal" / "Vishisht Seva Medal": different, Oct 7 2026). Model "contradict"/"same" judgements count only for statements read
  before frames (those still go through `check_conflicts`). Stored in `claims.rel.frame` and
  `canonical.rel.frame`.
- **One structure for the same fact (owner, Oct 7 2026; `relate.py`).** Replaced the veto chain
  (frames vetoing identical text, `may_be_same`, the NUM veto, frame auto-merges): two identical lines
  from two outlets stayed two statements because two readings labelled them differently. The words
  outrank the labels. `relate.relate(a, b)` decides by code on numbers (as numbers, ordinals, "eleven"),
  names (capitalised words; the names both lines share are left out of the word overlap) and root words:
  same / a covers b / ask / ask-covers / different; one negated and one not, or different times, is
  always different. Code merges only "same"; "ask" goes to `match.same_facts` (asked twice, A/B swapped)
  and "ask-covers" to `match.covers_facts` (asked twice, reversed order); proposals from the
  consolidation model and the frames go through the same question. A detailed line COVERS a short one
  when it has every number, name and nearly every word of it (code alone only when it adds no number
  and no word like "another", "earlier"; else the model is asked): the short line is not merged (its
  outlets would lend support to details they never reported) but kept as covered
  (`analysis.covered`), folded into the detailed line by `compose.fold_covered` (its outlets listed as
  sources, the detailed line's colour unchanged). Last net: `narrative._drop_repeats` drops a written
  sentence that an earlier one already says (its ids join the earlier one; a colour can only get
  weaker; a later sentence in the same paragraph that says all of an earlier one and more replaces it).
  Both `match.py` (arrival: code "same" only) and `consolidate.py` use it. Disputes are a separate,
  later step (frames still propose contradictions).
- **A sentence in coloured parts (owner, Oct 7 2026).** A sentence joining statements of different
  statuses is written in at most two "parts" (main fact first, split at a comma or "and"), each citing
  only its own statements; the site colours each part, the source numbers follow the sentence. Code
  checks every part on its own (`narrative._part_ok`: its numbers, names and most of its words from its
  own statements; the parts together are the sentence and all its ids); any failure removes the parts
  and the sentence has one colour, the weakest, as before. A covered short line that is BETTER supported
  than the detailed line is not folded away: the detailed line gets `adds_to` and the writer is told to
  write the two as one sentence in two parts. Parts mature like sentences (`editions.mature`); the
  Hindi page has no parts (one colour per sentence); the said-chain rewrite skips sentences in parts.
- **News first (Oct 6 2026):** the writer opens with what makes it news today (the newest or most
  consequential act or statement), never the setting; the headline is written AFTER the article from
  its opening paragraph (`compose._headline(lead=...)`), with the same checks.
- **Three relations:** same / contradiction (both cannot be true as facts: amber) / RESPONSE (a party
  answers a claim or finding: both true as reports, written together, never amber). "No denial" in
  the green rule means nobody denies the EVENT happened, not that a party objects to it.
- **Essay integrity:** a sentence leaning on the previous one ("He added", "denied this") falls with
  it; a statement and its contradiction/response are both in the essay or both under it; statements
  joined in one sentence must share a subject; "Also reported" skips what the essay already says;
  one repair call rewrites failed sentences before anything is dropped. Headlines must keep who did
  what (`compose._actor_problem`). Reports naming different actors for one fact block green; code checks every such model call: names
  appearing together in one sentence of a statement or report, not joined as aliases ("alias", "urf",
  brackets), are two people and never "named in reports as" each other (`consolidate.named_together`,
  Oct 7 2026: two arrested men written as one).
- **Everything in the article, in sections with headings (owner, Oct 5 and Oct 7 2026):** code puts every
  statement in exactly one section (`narrative.assign_sections`), and code puts them in this order
  (`narrative._in_order`, owner Oct 7 2026: a reader with no prior knowledge gets the context first):
  news (1-2 sentence lead, no heading), Background, Explained, What happened, By the numbers, What they
  say, Related events, What next. NO
  disputed section (owner, Oct 7 2026): a disagreement is written where its subject is, with both
  versions and whose; contradicting statements and claim/response pairs share a section; purple
  one-outlet lines stay with their subject (the colour marks them). One sectioned draft,
  then a FILL pass: each group of sections (news+happened+numbers / say / background+related+
  explained+next) that left statements out or has failed sentences gets its own small writer call,
  kept only if the article then carries at least as much (Flash-Lite given all 40 statements wrote 10
  sentences; a few at a time it uses them). `essay_ok` needs 85% of ALL statements (owner: the middle
  way). `narrative.section_keys` (one per paragraph) drives the headings on the site, English and
  Hindi. Leftovers stay in `narrative.not_in_essay`. **A short article is finished, not thrown away (owner, Oct 7 2026):** each try runs up to two fill rounds; a draft still under 85% is kept in `stories.analysis.writer_draft` (sections + model) and the next try resumes it with fills only (no new draft call, up to three fill rounds); new statements count as missing and are filled in; statements merged since the draft are followed through one report behind each (`writer_draft.anchors`, `compose._remap_draft`), so their sentences are kept; the draft is removed when the article publishes. (`recolour`/`needs_rewrite` unused: articles are
  closed.)
- **Introductions:** every person and body at first mention with the fullest name and role the
  statements give (`narrative._people` lists them for the writer); extraction names people in full.
- **Attribution like a newspaper:** name a speaker once, continue with "he said" in the same
  paragraph (validator honours the paragraph's speaker scope); a dispute states both versions and whose they
  are, never "other reports differ".
- **Red verdicts on hold** (`SETTINGS.model_verdicts = False`): no second model family on the free
  tier now Gemma fails most calls. Code verdicts still run. Tests keep the machinery on (conftest).
- **Headlines:** ≤12 words, one hammer-blow fact, people introduced by role, hook from the facts, no
  "reports say", no tacked-on "reportedly".
- **Threads:** a later development links to its earlier story (parent → daughter, many-to-many).
  Daughter opens with the new development + ≤2 background sentences + "Earlier in this story"; the
  parent gains no link (it is closed). Archived parents are found through the branch index.
  Front page: one entry per thread (its latest development), NEWEST FIRST by publication time (owner,
  Oct 6 2026; was ranked by importance and read as random); top 20, then "More stories". Filler is never published. A thread timeline page is a possible later step.
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
perspectives → origins + fact/characterisation → verdicts (`verify.py`) → settled? (`editions.py`) →
colours of published articles mature → retention → health checks in `runs.stats.health` → (workflow
step) articles older than 3 days to the archive branch. Rating (`priority.py`) comes after grouping;
search, Tavily reads, reading and analysis cover the preparation queue only.
Writing desk (`desk.py`, :45 UTC): settled stories, most important first → follow-up? → page, written once
(`compose.py`: importance, threads, headline; `narrative.py`: the essay; headline from its lead) →
Hindi. At most 2 per clock hour.

## Known quotas and facts learned from real data
- Google counts **each text in an embedding batch** as one request: ~1,000 texts/day per key.
- Flash models: 20/day each per key (writer/judge); Flash-Lite 500/day each per key; Gemma is
  unreliable on the free tier (overflow only). Grounded search ~20/day per model per key.
- **Refused (503) calls appear to count against Google's daily limit** (Oct 5 2026: gemini-3.6-flash
  key 1 had 0 successes all day and got Google's own per-day 429 after 13 refusals). A model refusing
  3 times in a row (any key) is dropped on every key for the run (`router.OVERLOAD_STREAK`), and the
  tier moves to its next model. Our counter still refunds refused calls; Google's 429 is the real limit.
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
- **Check next session (owner, Oct 7 2026):** do resumed drafts' extra fill rounds use up Flash writer quota early (desk `diagnostics` kind 'desk', `tier_calls`)? If Flash runs out by afternoon, drop the third round for resumed drafts first.
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
  gated off).
- **Perspectives must pass the daily positions test (`positions.py`, owner Oct 5 2026).** Like with
  like only (same story, same fact); evidence: OMISSION as the main signal (each side leaves out
  what is inconvenient to it; a fact counts as left out only if 2+ independent sources report it and
  `textmatch` finds its names/numbers absent from the outlet's text, across Roman/Devanagari),
  stance, framing (Hindi loaded words mapped to English concepts, `concepts.py`), voices. Model: 1-D
  ideal points; signal only if stable under resampling (r>=0.7), 15%+ of outlet pairs separated,
  and clearly better than outlet names shuffled per story. No signal, no perspectives at all.
  Offline on Oct 5 data: no method beat the shuffled baseline (too few multi-outlet stories).
  Clusters on voice kinds or raw words were rejected: they found genre and language, not lean.
- **Read in depth:** a story is read once 3+ independent sources have a readable page
  (`min_sources_to_read`); stories already being read are finished first.
- Writer failures on big stories: the reason is now split ("empty sentence" / "no valid ids" / "too
  long", plus reply-level `failure`); read `stories.analysis.writer_failures` before shortening input.
- Fallback sentences append `when_text` that can be relative to another event ("on Thursday, a day
  before his arrest" on the arrest itself); prefer the absolute date in fallbacks.

## Fixed at the root (Oct 2026), keep the guards
- Story 12687 (Oct 7 2026): (1) `style.py` shortened "Commission for Air Quality Management" to
  "Commission for Management" ("Air" read as a title): only PERSON_TITLES count as evidence of a person,
  and institutional words (Management, Commission, Quality...) are never a person's name; (2) a page's text
  about its own publisher ("Hindustan was established in 1936 ...") is dropped by code
  (`compose.drop_outlet_self_talk`) and extraction is told to ignore it; (3) every article opens with a
  news lead: a missing "news" section triggers the news fill, and failing that the first "What happened"
  paragraph becomes the lead; (4) "By the numbers" takes figures only, never years or dates
  (`narrative._is_figure`); (5) a context line (background, related, explained, next) must share a
  specific word or name, by root, with the story's own event, or code drops it
  (`compose.drop_unrelated_context`; common words like India, government, said do not count).
- One spelling per name in an article by code (`spelling.py`, Oct 7 2026: Machhar / Machar / Matchar /
  Machchhar on one page): capitalised words with the same key (tch/chh = ch, ee = i, oo = u, doubled letters
  single) take the spelling most reports use; applied to statements before writing and to the article and
  headline after. The consolidation model's `names` alone missed these.
- The same news is never published twice (Oct 7 2026: one Supreme Court order published as 11593 and
  11663 a minute apart): the thread check also asks for earlier articles reporting the SAME news and
  links them like a parent, so the story must pass `follow_up_ok`; it re-checks whenever an article was
  published since (`analysis.thread_checked_at`), and an article published first can be the parent
  whichever story was opened first.
- Name consolidation maps only spelling variants (`consolidate.is_spelling_variant`); aliases were
  rewriting "X, also known as Y" into "Y, also known as Y". Old bad mappings are undone on the next run.
- `when_text` is extracted in English; `narrative.english_when` converts any stored Devanagari.
- Writer validator rejects Hindi in English text, and reported speech ("A said that B claimed X")
  turned into a fact or pinned on A.
- `compose.tidy` removes "X (X)" duplicates.
- Run stats: `rated`, `queue`, `settled`, `colours_matured`, `live_pages`; the desk's own stats are in
  `diagnostics` (kind 'desk': ready, tried, published, tier_calls). Health flags a writer with 0
  successes in the desk's last 2 runs and two clock hours without an article while stories are ready
  (`run.writer_silent`). `published.updated_at` is the publication time.
- The headline model gets statements dated and newest first (`compose._newest_first`); sorted by support,
  old background outranked the new development and got tied to it with "after".
