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
  from when the rule was first met (before that: "developing", dotted teal).
- Brown = not yet cross-checked (owner, Oct 9 2026: teal for developing and clay brown for unchecked, so
  neither is grey and the two cannot be confused; the colour bar has a teal segment of its own); amber = sources disagree; red with a FALSE tag = primary evidence
  (FIR, court record, official data, video) shows it false, two model families agreeing.
  **Purple = one outlet only (owner, Oct 7 2026):** a statement only one independent outlet reports
  (and not disputed or false) goes INTO the article, shown purple ("one outlet only": an exclusive,
  or a mistake); the colour, not words, says so (see "One author's voice"). `narrative.shade`; the writer and the revision pass must
  use every statement, one-outlet lines included (they were "minor" and optional before).
- **Interim publishing rule** while perspectives are unknown: 3+ independent read outlets (state media
  and PIB not counted, see "Outlets outside India") and 2+ origins. Perspectives emerge from agreement data (no hand labels of outlets).
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
    result) carried by 3+. No per-day limit (owner, Oct 10 2026: it was 5+ major-only on the parent's IST date): the same bar holds all day. The desk (`desk.ready`) writes stories whose topic has NO article published today first (`fresh_topics`, topic = root of the story_links / `edition.follows` chain), then the rest by importance, so a topic already covered today is never held back, only queued behind new topics.
  - After 3 days the article moves to the `archive` branch (`pagearchive.py`, step in hourly.yml:
    `pages/<id>.json.gz`, `index/<YYYY-MM>.jsonl`) and is deleted from Supabase only after the push.
    The site and follow-ups read archived parents from the branch (raw.githubusercontent.com).
- **Writing desk and preparation queue (owner, Oct 6 2026).** Target: at least one article an hour,
  at most two. The writer is its own step (`desk.py`; since Oct 10 2026 in hourly.yml right after the
  pipeline, which starts :05 UTC = :35 IST; before, workflow `writer.yml` at :45 UTC): it writes the
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
- **Grammar, not rules (owner, Oct 10 2026; direction agreed, built in phases).** Prohibitions added after each failure
  fail more than half the time with the small models, and a failed sentence is dropped, so more checks can lower the
  publish rate. Direction: CONSTRAIN WHAT THE WRITER IS GIVEN (code owns names, paragraph plan, sentence shapes; the
  model only phrases) rather than only checking what it wrote. Two independent grammars: (1) by statement status
  (established / developing / unchecked / one outlet / disputed / false: allowed sentence shapes and markers per
  status), (2) by article structure: article > sections > paragraphs > paragraph links > sentences > words (titles
  and names included). Only words, names and sentence shapes are checkable by code; "flow" is not a rule, it comes from
  a sound paragraph plan (one subject per paragraph, set order).
  **Phase 1, done: titles registry** (`config/titles.yaml`, `titles.py`): one list of honorifics and roles with
  synonyms (`CJI` = `Chief Justice of India`), kinds (`is_a`: a CJI is a Supreme Court judge, never the same) and
  conditions (`requires_any`: "Chief Justice" alone is the CJI only in a Supreme Court / India text). `style.py`,
  `people.py`, `voice.py` take their title words from it (they used to keep three private lists). `style.shorten_names`
  now owns every mention by code: the FIRST mention is the full introduction (title + full name, the title taken from
  the statements), a bare surname or "Title Surname" before that is expanded to it, every LATER mention is the surname
  alone, whatever the writer wrote; full names come from the statements too, not only from the article text.
  **Phase 2, done (owner: "yes let's build", Oct 10 2026): grammar by statement status** (`grammar.py`, no model in it).
  One table `STATUS` (established / developing / unverified = not cross-checked / single = one outlet / disputed /
  false): what the colour already says, what the words may carry (the four plain statuses: plain declarative,
  attribution only for the statement's own named speaker; disputed: both versions each with its holder; false: the
  claim, who made it, what the evidence shows) and the markers required. The marker lists (`FALSE_MARKERS`,
  `DISPUTE_MARKERS`, `ATTRIBUTION_VERBS`, `HEDGE_MARKERS`) and `CONNECTIVE` moved there; `narrative.py` re-exports them
  (`recap.py` imports them). Three uses: (1) GIVEN to the writer: `_statement_line` adds `| SHAPE: ...` to a statement
  that is not plain (`grammar.shape_line`: "<content>, X said.", the statement's own act verb, the false-claim form
  with its evidence, the two-version form); a plain statement gets nothing; both prompts say a SHAPE is the form to
  use. (2) APPLIED by code: when a sentence fails a check and the fault has ONE safe fix, `narrative._check_one` applies
  `grammar.repair` and runs EVERY check again; the repair is kept only if the sentence then passes, otherwise the original
  failure stands (a repair can turn a dropped sentence into a published one that passes, never relax a check). Repairs:
  `not a full sentence` -> add the full stop (never on a fragment: a joining word first, cut off on one, or under 4
  words); `claim without its speaker` -> ", X said." after the claim (one speaker, no other speaker among the
  statements, and the sentence has no speech verb of its own: a wrongly attributed sentence is not given a second
  speaker; for a false claim it goes before the evidence); `false without saying so` -> "The evidence shows this is false:
  <the statement's evidence>." (only when the false claim is the sentence's only statement); `verb the statements do
  not use` -> "said that" for claimed/insisted/warned/threatened/conceded/admitted/confessed/vowed that, NEVER for a verb that
  negates (denied/refused/rejected/dismissed that); `pronoun without evidence` -> the speaker's name for "He/She said"
  (the names pass writes it as full name or surname). A repaired sentence loses its colour parts (one colour, the
  weakest). The counters show it: a repaired fault is counted as `fixed: <reason>` in `reject_reasons` /
  `first_draft_reasons`, not as a rejection (read them with `tools/replay.py`: a high `fixed:` count means the writer
  keeps making that fault and the prompt shape is still not holding). (3) one new check: `empty set-up` ("X set out his
  position.": cites a statement, says nothing of it; short, no figure) is refused so the fill pass writes the statement.
  `WRITER_VERSION` was NOT raised: old pages keep their text (a raise rewrites every page once, a quota cost); new and
  rewritten pages get the shapes and repairs. Not run on real stories (no data in the repo); the unit tests ran as plain
  Python (the sandbox had no pytest / sqlalchemy), see `tests/test_pipeline.py` (grammar tests at the end).
  Not yet built (next phases, in this order): measure which rules fail most on stored data (`tools/replay.py`; now
  `fixed:` counts show what the repairs catch); paragraph plan in code (one call per paragraph group, with the previous
  paragraph's last sentence; sections' own templates); titles learned from outlets (a title seen before a name in 2+
  independent outlets enters the registry, with dates; needs a migration if stored in the database).
- **Story layers (Oct 5 2026, owner):** a story has its own event (core) and CONTEXT: background,
  related events (a separate event the reports connect to this one: written as separate, never
  blended), explanation, reactions, what next. Extraction records context (`claims.rel.context`),
  consolidation can re-label statements (`analysis.roles`). Headline, timeline and essay_ok use the
  core only; context is written after it and coloured like everything else.
- **Disputes: one gate (owner, Oct 7 2026; `disputes.py`).** Replaced nine paths (frames adding
  contradictions on arrival and deciding them in review with no check, the one-shot "contradict" question,
  `real_difference` / `typed_difference`, the model's own list, "same" lines with different numbers): "17 of
  the 19 crew are Indian" / "11 of the 12 injured are Indian" was shown amber. A dispute is the SAME
  QUESTION with a DIFFERENT ANSWER. (1) Code (`disputes.candidate`): the speaker taken off ("Police said",
  "according to"), the answer taken out (numbers, number words, dates, weekdays, "not"); what remains
  must be nearly the same words (with `relate.SYNONYM`) and the same names; rounding, bounds ("at least"),
  and "X of Y" with different totals are cleared by code. Proposals (the consolidation model, the frames,
  earlier marks) pass the same gate. (2) Figures over time: when every report of one figure was
  published at least 1 h after every report of the other, it is an UPDATE (`analysis.updates`, old ->
  new), not a dispute: written together, the newest first, the older mentioned; each keeps its colour.
  (3) `match.check_conflicts` asked twice, A/B swapped: two "cannot both be true" = amber (however many
  outlets on each side; an official against an outlet is a dispute too); any "unsure", the answers
  disagreeing, or not yet asked = doubtful (`analysis.doubtful_conflicts`: no amber, no green). Answers
  cached per ordered pair in `analysis.conflict_checks`. Claim/response pairs are never disputes. Only
  the story review makes disputes; arrival (`match.match_story`) only joins plainly identical wording.
  Outlet denials (a report that denies a claim, `verify.py`) are a separate relation, unchanged. The
  writer puts a real dispute with its subject, never in a closing "accounts differ" paragraph.
- **Frames: statements compared field by field (owner, Oct 6 2026; `frames.py`).** Reading gives every
  statement who / action (base verb) / what / value / where / negated; words are reduced to roots
  (Snowball stemmer), numbers read as numbers (crore, million, "at least", "about"). Code compares:
  same slot (who, action, what) + agreeing values = one fact (merged); same slot + incompatible values
  or negation = contradiction (the ONLY way to one between statements with frames); a missing value =
  compatible (never a dispute, wording decides merging); same slot at different times = unsure;
  anything else = different. Frames now only PROPOSE, for merging and for disputes; they decide
  nothing (see "One structure for the same fact" and "Disputes: one gate"). A day-precision date covers the whole day (`_times_apart`). Dates are read as dates ("6 December 1986" = "1986-12-06"; a year agrees
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
  always different BY CODE (a pair the topic step proposes is still asked: see "The same fact in other words"). Code merges only "same"; "ask" goes to `match.same_facts` (asked twice, A/B swapped)
  and "ask-covers" to `match.covers_facts` (asked twice, reversed order); a short synonym list
  (`relate.SYNONYM`: injured/hurt/wounded, arrested/detained, part/section...; never injured/killed)
  is read as one word; proposals from the
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
- **One fact per statement (owner, Oct 8 2026; `split.py`, story 13970).** "The repo rate is the rate at which
  the RBI lends to banks" was written three times, the rate decision four: outlets write compound sentences
  (A = shared fact + x, B = shared fact + y), neither covers the other, so the shared fact stayed in two
  statements. Before matching, in preparation, a fact row that looks compound (14+ words and a joining
  "and", ", which", ";", " but "...) is split into its single facts by one Flash-Lite question per batch of 10
  (analysis tier since Oct 9): at most 3, 4 allowed, more = kept whole; one fact = unchanged. Code checks every piece: its
  numbers and names from the original, nearly all its words too, the pieces together carry every number and
  name, a said thing keeps its speaker in every piece, a "not" is never lost or added, and no two pieces share 60%+
  of their root words (a list split apart: "higher EMIs for home loans" / "... for car loans", seen on 13970);
  any failure keeps the row whole. Pieces replace the row (same article, stance, speaker, evidence, time, context role; loaded words
  on the first piece; no frame; a relation naming the row names its first piece), marked `rel.split`
  ("piece"; checked rows "whole") so a row is asked once, and go through matching like any fact. Numbers:
  "25 basis points" reads as 0.25 per cent (`frames.numbers`). Writing: a sentence may now have up to THREE
  coloured parts (`narrative.MAX_PARTS`). `replay --mode split` shows a stored story's statements after.
- **The same fact in other words, found by topic (owner, Oct 8 2026; `dupes.py`, story 13970 after the split:
  "rate cuts were off the table" / "no option for interest rate cuts"; "by 0.25 percent" / "by 25 basis points to
  5.50 per cent").** Deciding was never the weak part; finding the pairs was (the review's seven-job call over 90
  statements found five). Now, in the review: one call sorts the statements into topics (a paragraph's subject),
  then per topic (up to 12) one call proposes "same" pairs and "covers" pairs (longer first). PROPOSALS ONLY: a
  same pair goes through the existing gate (relate by code, then `same_facts` asked twice); a covers pair must
  keep the short line's numbers and its "not" (code), then `covers_facts` asked twice. Topics and answers kept by
  TEXT in `stories.analysis.dupe_checks` (ids change after merges). `CONSOLIDATE_VERSION` 9: every unpublished
  story is reviewed once more. A "no" does not keep a proposed pair apart (owner, Oct 8 2026: "if they mean the
  same thing, we should not leave them unmerged because one has no in it"): a pair the topic step proposed is
  asked even when one side is negated, and the twice-asked same question decides; its examples hold the trap
  ("Police arrested him" / "did not arrest him" -> different; "rate cuts are off the table" / "no option for
  rate cuts" -> same). Numbers must still agree. Code alone still never merges a negated and a plain line.
- **Write facts, cite outlets (owner, Oct 9 2026).** "It's fine if we leave out lines from outlets if they have
  already been covered from other outlets and just give their number." The article tells each FACT once:
  completeness (`essay_ok`, 85%) counts facts, not statements. A line whose fact another line already tells is
  FOLDED (`analysis.covered` -> `compose.fold_covered`): not written, its outlets' numbers go on the telling
  line's sentence, which keeps its OWN colour (never raised by them). Folding needs ONE model "yes" (same fact,
  or the line says everything the other does: the first answer of the existing twice-asked questions, recorded
  as "f..." in `same_checks`) plus code: every number and name of the folded line is in the telling line
  (`consolidate.tells`); the telling line is the better supported, then the longer. Merging statements (which
  changes colours) still needs two "yes". Two sides of a dispute and an old/new figure are never folded. A
  sentence may carry facts of different colours in up to 3 parts (owner: "each sentence can have multiple
  colours"). Splitting stays: on 13970 one definition sat inside three different compound lines, which no fold
  can join. `CONSOLIDATE_VERSION` 11.
- **Paragraphs of a readable length, by code (owner, Oct 9 2026, story 13792: seven one-sentence paragraphs,
  one of 14).** `narrative.shape_paragraphs`, after the writer and the coherence rewrite: within a section,
  neighbouring paragraphs of up to 2 sentences are joined (up to 4); a paragraph over 6 sentences is cut into
  paragraphs of 2-5 where the speaker or subject changes, never before a sentence leaning on the one before;
  the lead is left as written; sentences are never changed.
- **What already happened is not written as scheduled (owner, Oct 9 2026, story 13792: "The 57th GST Council
  meeting is scheduled to take place ... on Thursday, October 8", written after it).** `compose.drop_past_schedules`
  before writing: a line saying something "is scheduled / set / expected to", "will be held / take place ...",
  dated before today (IST), is left out; dated today, only when another line reports a decisive act with the same
  names. A line with no date is kept (code never guesses).
- **"Separate" only when the outlets say it (owner, Oct 9 2026, story 16000: "In a separate case, ..." printed for
  the story's own case, a line reading had filed as a related event).** Like an act verb, the word is the outlets':
  `narrative._no_separate` (in `_finish`) removes "In a separate case,", "Separately,", "in an unrelated incident"
  unless the statements behind the sentence use "separate"/"unrelated"; the "Related events" heading tells the reader
  it is another event, and the writer is no longer told to mark related events as separate.
- **"Explained" keeps what reading labels explanation (owner, Oct 9 2026):** a decision filed there is not moved
  ("better more than less").
- **A sentence is a sentence (Oct 9 2026, story 13792: lead "An unnamed source said on Thursday."; "... on
  Thursday," then "And tax officers ...").** Pieces the writer returns apart are joined by code
  (`narrative._join_fragments`: a piece ending in a comma joins the next; a lower-case piece joins an unfinished
  one; different statements = coloured parts); a lower-case slip after a full stop is capitalised; anything that
  still does not start and end like a sentence is refused ("not a full sentence") and the fill pass rewrites it.
  A speaker's surname twice in one sentence is a repeated name ("Sitharaman said ..., and Sitharaman said").
- **A sentence in coloured parts (owner, Oct 7 2026).** A sentence joining statements of different
  statuses is written in at most two "parts" (three since Oct 8 2026, see "One fact per statement"; main fact first, split at a comma or "and"), each citing
  only its own statements; the site colours each part, the source numbers follow the sentence. Code
  checks every part on its own (`narrative._part_ok`: its numbers, names and most of its words from its
  own statements; the parts together are the sentence and all its ids); any failure removes the parts
  and the sentence has one colour, the weakest, as before. A covered short line that is BETTER supported
  than the detailed line is not folded away: the detailed line gets `adds_to` and the writer is told to
  write the two as one sentence in two parts. Parts mature like sentences (`editions.mature`); the
  Hindi page has no parts (one colour per sentence); the said-chain rewrite skips sentences in parts.
- **News first (Oct 6 2026):** the article opens with what makes it news today, never the setting;
  since Oct 8 code chooses it (see "The news, the lead and the headline").
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
  way; since Oct 9 2026 folded lines are not counted: facts, not statements). `narrative.section_keys` (one per paragraph) drives the headings on the site, English and
  Hindi. Leftovers stay in `narrative.not_in_essay`. **A short article is finished, not thrown away (owner, Oct 7 2026):** each try runs up to two fill rounds; a draft still under 85% is kept in `stories.analysis.writer_draft` (sections + model) and the next try resumes it with fills only (no new draft call, up to three fill rounds); new statements count as missing and are filled in; statements merged since the draft are followed through one report behind each (`writer_draft.anchors`, `compose._remap_draft`), so their sentences are kept; the draft is removed when the article publishes. (`recolour`/`needs_rewrite` unused: articles are
  closed.)
- **Who speaks is code's job (owner, Oct 8 2026, story 13107; `voice.py`).** "Humayun Kabir added that ... Humayun
  Kabir also stated that ... Humayun Kabir further stated that ..." came from four layers each patching one
  sentence. Now: (1) a statement "X stated/said that Y" is given to the writer as Y, said by X (`voice.split`,
  neutral verbs only; "accused X of", "denied" keep their verb); (2) the VERB is the outlets': "said" unless the
  statement itself uses a stronger one; the validator rejects "accused", "denied", "threatened", "claimed"... the
  statements do not use ("verb the statements do not use"); (3) PRONOUNS only with evidence (owner: a wrong one
  harms a real person): two outlets' statements use he/she for the person, no other person named in them (bodies
  like "police" are fine, "a sub-inspector", "his son" are not), none use the other; otherwise the surname or the
  role ("the MLA", only when one person has it). The validator rejects an unsupported pronoun ("pronoun without
  evidence") unless the statements behind the sentence use that very pronoun; code never writes one without
  evidence; (4) a speaker named in one paragraph carries into the next paragraph of the same section; (5) a
  paragraph's coherence is measured by code (`voice.problems`: one speaker named in 3+ sentences, "also stated /
  further stated", the same opening twice, sentences over 45 words, 7+ sentences); it decides the coherence
  rewrite AND whether the rewrite is kept (it must have fewer problems: a rewrite that split one block into four
  paragraphs, each opening with the full name, had been kept); (6) the code floor (`style.vary_attribution`,
  `style.Refs`): a run of one speaker becomes "..., he said." / "..., the MLA said." / "..., Kabir said." (rotated;
  "he" only with evidence), and "also stated"/"added" opening a paragraph becomes "said". Sentence-opening words
  ("While", "Meanwhile", days, months) are never part of a name (`style.LEAD`: "While Humayun Kabir" had stopped
  the surname from being used).
- **One speaker's argument told together; the attribution is the statements' speaker (owner, Oct 9 2026, story
  16197).** "The Centre alleged ... overreached the court's judgment" and "The Central government alleged ... less
  than six months" sat in two paragraphs with other speakers between and read as two different claims; the Centre's
  line was printed as "..., the advocate alleged" and the Supreme Court's as "..., she said" (style read "The ..."
  and "He" as whoever spoke before; on the 30 latest articles also Scindia's figures as "Modi said", Rahul Gandhi's
  lines as "Kharge said"). Now: (1) `voice.speaker_key`: one key per speaker (the Centre = the Central government =
  the Solicitor / Attorney General; the Supreme Court = the bench = the CJI; a person by surname); (2) the writer gets
  "What they say" grouped by speaker (`narrative._by_speaker`, an answer right after what it answers) and is told to
  write each speaker's lines in one paragraph; (3) code regroups a "What they say" section whose speakers are
  scattered (`narrative.group_speakers`, sentences unchanged, before `shape_paragraphs`); (4) the validator refuses
  "wrong speaker" (`voice.wrong_speaker`: the name a sentence gives must be the statements' speaker, an alias, or
  a role of that person; never a role for a body; he/she only for that speaker with evidence) and "speaker named
  twice" (`voice.double_attribution`); (5) `style.vary_attribution` refers to each sentence's own statements'
  speaker, and a body is never "he" / "she".
- **A body's run of lines (owner, Oct 9 2026, story 16708: "The US Department of State said that ..." three times
  in a row).** People already got "..., he said." / "..., Kabir said."; bodies got nothing. Now (`style.vary_attribution`
  body branch, `voice.is_body`, `voice.body_refs`): in a paragraph, the second and later lines of the same BODY (by
  its statements' speaker key) end "..., the department said." / "..., it said." (rotated; a plural body: "...,
  they said." / "..., the officials said."), and "The US Department of State identified ..." becomes "The department
  identified ..."; never when the line opens with the body's own pronoun ("its primary activity"). A foreign
  government is one speaker however named (`voice._country_gov`: "US Department of State" = "US officials" = "the
  United States government"). People are unchanged: he/she only with evidence, else the surname or role (singular
  "they" for a named person was not adopted: it reads as plural next to bodies). A person missing from the name
  list is referred to by surname, never "the Raghoo Puri".
- **Introductions:** every person and body at first mention with the fullest name and role the
  statements give (`narrative._people` lists them for the writer); extraction names people in full.
- **Attribution like a newspaper:** name a speaker once, then the surname, the role, or "he"/"she" only
  with evidence (see "Who speaks"); a dispute states both versions and whose they are, never "other reports
  differ".
- **Red verdicts on hold** (`SETTINGS.model_verdicts = False`): no second model family on the free
  tier now Gemma fails most calls. Code verdicts still run. Tests keep the machinery on (conftest).
- **The news, the lead and the headline: one structure (owner, Oct 8 2026; `news.py`).** The news is
  chosen ONCE by code (`news.pick_news`, no call): the story's own statements (not context, not FALSE),
  ranked: not old (an older year, "previously", "had announced" = the past) > a decisive act (deaths
  first; verbs only: arrested, ordered, signed... over held, met, heard; "not" = -1) > HOW MANY OUTLETS tell
  it (a fact's outlets: its own, folded lines', and statements telling the same act in other words) > dated
  on the story's newest day or the day before > central (shares the story's subject) > names and numbers.
  Outlets before the date since Oct 9 2026 (owner; story 13792: a vague one-outlet line dated today beat the
  arrest-power decision three outlets carried). The lead can carry TWO facts (`news.lead_news`, payload
  `lead`): the best-reported one, and when that is not today's, the best decisive fact dated today (not the
  same act in other words), told in the second sentence; the lead must cite the first. "The same act in other
  words" (`news._same_act`): same decisive verb AND a shared number, or a shared name and most of the smaller
  line's words; a shared name alone made every "the GST Council approved ..." line tie (13792 replay). A line one
  outlet tells never leads over a fact 2+ outlets tell, whatever its verb (story 13058, Oct 9 2026: "Nana Patekar ...
  won millions of hearts", one outlet, led the India Mobile Congress story); among 2+ the act still ranks before the
  count (outlets first lost deaths and a bounty on the 30 latest articles). "inaugurated", "unveiled", "issued" are acts. Checked on the 30 latest
  articles. The writer gets it as SECTION news; the lead must cite it (`narrative.lead_ok`, else the news
  fill rewrites the lead). The headline is written after the article (`news.write_headline`): one call
  gives three candidates; code rejects (>12 words, any "reportedly"/"reports say", bare name, loaded word,
  cause word, number or name not in the statements, who-did-what, a DISPUTED figure stated as fact:
  disputes may be the news, owner Oct 8, but the headline says whose version or leaves the figure out)
  and scores the rest on the six principles (the outcome not the process, one concrete detail, a known
  name or role, 7-12 words, no jargon, about the news); a second call only if all three fail; no
  headline = the article waits as a kept draft. House style by code: PM, CJI, CM. Removed: the draft
  headline before writing, the ESTABLISHED/REPORTED labels and "must hedge" rule, the "reportedly"
  fallback, and the per-story importance call (front-page rank = `priority` score + coverage; filler is
  filtered by `priority`). Headline calls per try: 2-7 before, 1-2 now.
- **Threads:** a later development links to its earlier story (parent → daughter, many-to-many).
  Daughter opens with the new development + ≤2 background sentences + "Earlier in this story"; the
  parent gains no link (it is closed). Archived parents are found through the branch index.
  Front page: one entry per thread (its latest development), NEWEST FIRST by publication time (owner,
  Oct 6 2026; was ranked by importance and read as random); top 20, then "More stories". Filler is never published. A thread timeline page is a possible later step.
- **Grouping:** one embedding model only (`gemini-embedding-001`, chosen on 216 labelled real pairs);
  join only on high similarity to closest members and the story's fixed core, a model checks the
  middle band, stories holding separate events are split. When unsure, keep apart. The middle-band
  question (Oct 8 2026, story 13968: a Ludhiana sarpanch killing of Oct 4-5 merged into a Tarn Taran
  sarpanch killing of Oct 6 on one question about two headlines) compares the article with the story's
  CORE article (IST date, headline, opening words; worked examples incl. the sarpanch trap), asked
  twice A/B swapped, two "same" join, unsure = different; a borderline article published 12 h+ before
  the story's first report (`story_before_hours`) is never asked in (an earlier event).
- **Reading site nishpaksh_version_1.0 (owner, Oct 8 2026; `site/`, the main address; `/v1/` redirects there).** Designed on a canvas
  (trial versions 0.1-0.6) and approved: one card at a time (swipe up/down, snap, loops newest <-> oldest),
  the card shows the headline then the article itself fading out, a colour bar (green / teal / purple / amber / red /
  brown shares of the article's coloured pieces) with the time beside it. **Version 1.1 (owner, Oct 8 2026, after
  reviews said the multicolour sweep and the rounded sans looked like Instagram):** header and article headline
  box = one indigo gradient, light to deep (#46549a -> #161a38); headlines Newsreader (Hindi: Noto Serif
  Devanagari); greeting upright Newsreader / Tiro Devanagari by IST time; wordmark Alfa Slab One / Rozha One
  (Hindi); EN/हिं switch. Tapping a card grows it into the
  article (clip-path from the card, headline glides into the gradient box); back gesture closes it. The
  article keeps everything the old page had (sections, colour key, source numbers, sources, threads, notes,
  framing, perspectives) and "Who reported what" (folded; outlets link to their articles). Mobile first;
  laptops get the same deck centred (a laptop design is for later). The old list-style site and the old
  Streamlit app were removed (owner, Oct 8 2026); links of the form `?lang=hi#/story/<id>` still work. Data: `nishpaksh/feed.py` writes `en.json`, `hi.json` (cards) and `story/<id>.json`
  (slim pages) to the `feed` branch after every desk and pipeline run (`.github/scripts/publish_feed.sh`,
  one commit force-pushed, `continue-on-error`); the site reads them from raw.githubusercontent.com and falls
  back to Supabase, then the archive branch. Service worker keeps the page and last news offline.
- **Site sections (owner, Oct 9 2026; `categories.py`).** Five primary sections, five secondary under each, chosen
  with the owner from ~490 rated stories (politics ~30%, crime/courts ~25%, world ~11%, accidents/weather ~8%,
  business ~7%, health/education ~7%, defence ~5%, films ~3%, sport ~1%: sport is too thin for its own section):
  Politics (Elections, Parties, Government, Parliament, Protests) · Justice (Crime, Police, Courts, Corruption,
  Terror) · Business (Economy, Companies, Markets, Your money, Domains > HR, Finance, Marketing, Analytics,
  Operations; Oct 9 2026, see "Domains and Sport") · World (Diplomacy, Indians abroad, Conflicts,
  Defence, Abroad) · Life (Health, Education, Accidents, Environment, Sport & films). Owner: short, catchy names;
  at most five of each (the site's bottom bar). An article gets up to 2 primary and 2 secondary
  (`payload.category`, keys, first = main; same in Hindi; labels in `categories.labels`, in en.json/hi.json
  `sections`, cards `cat`). One page call at publication (headline, lead, top statements; asked once: a section
  colours and merges nothing); code keeps only listed keys, a secondary brings its primary, unsure = primary
  alone; no answer never holds an article back. Live articles from before sections get theirs on desk runs
  (`fill_live`, 5 per run, only the section is added). **Site display (owner, Oct 9 2026, canvas "Sections A"):** a
  bottom bar in the header's indigo, joined to the bottom edge, rounded on top: All + the five primaries. Tapping a
  primary slides it to the LEFT end where All was, it becomes the back button (‹), the others give way to its five
  secondaries; a secondary filters and the bar stays (tap again = the whole primary); back = all. Labels on one line,
  measured, one font size for every row, equal space between labels; sections with no story yet are lighter and open
  an empty card ("Nothing in X yet" + Show all news). The cards fade and restart at the section's newest. The bar
  slides down off the page while an article is open. Filtering is client-side over every card in the feed.
  **Back to the latest (owner, Oct 9 2026, canvas "Latest 3b"):** two cards or more from the latest (one, in a section
  of two or three; either way round the loop), the CHOSEN button (All, ‹ section, or a sub-section) grows a little and
  shows an arrow at its right end, pointing to where the latest is (down when swiped up past it). Its left half does
  what it always does (back; a sub-section turns off); its right half goes to the latest (`#sb-latest`, a touch area
  over it); All: the whole button. Tapping the wordmark also goes to the latest. Bar classes are prefixed `sb-`: a
  class named "back" once picked up the article back button's shadow. Share: a button at the right of the article's
  sources and time line (the phone's share sheet, else the link is copied).
  **Depth and motion (owner, Oct 9 2026, canvas "Swipe depth 1 — Tilt"):** while swiping, the card leaving tips back
  (rotateX up to 10 deg, back 120 px, scale 0.94, opacity 0.65) and the next rises from slightly behind
  (`paintDepth`, on the slots, only the 3 or 4 near the view; at rest nothing is moved; off for reduced motion). Back to
  the latest RUNS through the cards in between (at most five, from five out when further; 260 ms + 90 ms a card,
  the short way round the loop), so it is seen to go there (`toLatest`). **The phone's back in a section** steps out
  one level (tertiary → secondary → primary → all) instead of leaving the site: every level gets its own history
  entry WHEN TAPPED INTO (`{np: "sec", level, sec, ter}`; deeper = push, sideways = replace, shallower through the bar =
  `history.go(-n)`), and back restores the section of the entry it lands on. Entries added while going back were
  skipped by Chrome on Android (no tap = no entry) and the site closed. An open article is closed first.
- **Pictures: collected first, shown later (owner, Oct 9 2026).** Principles agreed: (1) only the outlet's own
  SHARE picture (og:image / twitter:image, else the feed's media:content / thumbnail / image enclosure), LINKED from
  the outlet and credited to it, never copied to us; (2) chosen by a SIMPLE code rule, no image analysis (owner, Oct 9 2026: "we
  don't need complex analysis, just the most relevant one"): the picture of the outlet whose report the lead comes
  from, else the next outlet of the story; an outlet's default logo picture (the same link across its unrelated
  stories, e.g. TASS) is never used. Licence-free Wikimedia pictures were offered and not chosen (generic, often none); (3) a picture that does not load leaves the
  card as it is. Stored in `articles.image` (migration `20261009000100`; "" = looked, none; NULL = not looked):
  `ingest.page_image` / `entry_image` at reading, `discover` for search finds, `ingest.fill_images` looks once at up
  to 60 articles of the last 36 h in a story that have none yet (run stats `images`: with / without / filled).
  **Shown (owner, Oct 9 2026, canvas "Pictures A" and "Article · Calm + tiles + quotes + rail"):** `feed.pictures`
  picks one per live article at every export (the lead's reports first, then the rest; a link on 3+ stories =
  logo) and puts it on the cards (`img`: src, by, href). Home card: the picture across the top, sharp, then
  blurred and washed to white where the headline begins, credit chip "Photo: <outlet>"; a story without one, or
  whose picture fails to load, keeps today's card. Article: the picture at the top of the indigo headline box,
  dissolving into it, credit linked to the outlet's article; the top stays (back · wordmark · language) with the
  wordmark where and as big as on the home page in the home page's indigo gradient; under it a rail of the
  article's sections (the one being read filled, a tap goes there) and a line in the same gradient that fills as
  you read; the lead alone in a larger serif; each section its own card under a numbered heading; By the numbers
  as tiles (the sentence's first figure big, the sentence under it, colours and source numbers kept); What they
  say one card per paragraph with its speaker (`feed._speakers`: the name the reports give most often,
  `narrative.who`, English page only). Display only: the closed article's text is unchanged. **Opening and closing animate by transform and
  opacity only** (Oct 9 2026, jitter on two Android phones): the headline box keeps its open layout, the headline and
  the picture are moved over the card's (`placeOverCard`: --tx/--ty, --pt scale covering the card's picture, --hw = the
  card headline's width so the lines break the same); never animate padding, width, height, top or left there (one
  relayout per frame: 33-40 per animation before, 3-9 after). The article's picture is laid out at its whole cover size
  (`fitPic`) and gets its own transform (`--it`) so it is at exactly the card picture's zoom and crop at both ends; it
  had been over-zoomed while closing and snapped back at the end (owner, Oct 10 2026).
- **No internal notes for readers (owner, Oct 8 2026):** the article page no longer shows "Nothing in this
  story is confirmed by independent sources yet" (read as "this story is fake"; the colours already say which
  parts are supported) nor "Which outlets form different perspectives is not yet known" (internal state, not
  something a reader needs). Keep such status out of the reader's page.
- **Address (owner, Oct 8 2026):** the repo lives in the free GitHub organisation `NishpakshNews` as
  `NishpakshNews/NishpakshNews.github.io`; the site is https://nishpakshnews.github.io ("nishpaksh" was taken). The
  page reads its feed/archive from that repo; `pagearchive` uses `GITHUB_REPOSITORY`; both Supabase dispatch
  functions call `public.dispatch_workflow` (migration `20261008000100_repo_moved.sql`) with the Vault token
  `github_dispatch_token`, a fine-grained token for this repo (Actions: read and write).
- **Egress (Oct 8 2026):** Supabase free = 5 GB/month; the first week used 12.5 GB, ~85% of it the pipeline re-reading
  article text, embeddings and minhashes every run. `heavy.py`: those columns are selected as md5 and served from a
  local SQLite cache (kept by actions/cache in hourly.yml, `NISHPAKSH_HEAVY_CACHE`), fetched only when new or changed;
  run stats `heavy_cache` (hit / fetched). Article choice reads text lengths, then the text of the chosen articles only;
  priority reads only stories with a fresh report; the feed re-reads only articles whose payload md5 changed
  (`manifest.json` on the feed branch). Keep new heavy reads behind `heavy.columns`/`heavy.fill`.
  **Oct 10 2026 (still ~1.7 GB/day after the cache; the org went over quota):** the thread check read every live
  page (~70 KB each) for every story it checked: it now reads only the headline and first two lead sentences in
  SQL (`threads._slim`); `editions.mature` reads a page only when its verdicts changed since the last maturing
  (`stories.analysis.mature_sig`, set in SQL with jsonb_set); per-story text reads (origins, perspectives,
  consolidate, extract) go through `heavy`; the desk (writer.yml) uses the same cache; the site tries the feed
  twice with 15 s before reading Supabase (5 s fallbacks read ~100 MB a day). Never select a `published` payload
  whole where a JSON path would do. Budget: 5 GB a month = ~165 MB a day for everything.
- **Domains and Sport: one rigorous route, reserved places (owner, Oct 9 2026).** A cheaper "brief" lane for business
  and sport (single source, no embeddings) was designed and REJECTED by the owner: one standard everywhere, same
  depth, same perspectives. Instead: (1) **Business = Economy · Companies · Markets · Your money · Domains**, and
  Domains has a THIRD level, **HR · Finance · Marketing · Analytics · Operations** (`categories.TERTIARY`;
  `payload.category.tertiary`; up to 2 of each level; "business/domains/hr" in the one section call). A domain is a
  LENS over the news, not a pile of news: a story is HR if it changes something for people at work, whatever
  else it is (Oct 2-9: only ~2-5 genuine stories a week per domain among 526 multi-outlet stories). Jobs and Tech
  became HR and Analytics (`categories.normalize` / site `normCat` map old keys; stored payloads unchanged).
  (2) **Reading queue:** of `prep_queue` (16), `prep_beat` (4) go to the best Domains/Sport stories; unused places
  go back (`priority.queue`). A story is a beat story when its desk question says so (`priority.beat_check`, its OWN
  call per 20 headlines, worked examples, unsure = none, asked once, again when coverage grows by 2+; saved in
  `analysis.beat`; scheduling only) or half its articles come from feeds tagged `beat: domains|sport`
  (`priority.is_beat`). Oct 9 2026: asked inside the importance rating it answered "none" for all 36 stories rated
  in its first 3 hours, among them Indian IT firms suspended from the US green card programme, the anti-cancer
  drug margin cap and the Starlink row, so the beat seat stayed empty every run. (3) **Desk seats:**
  at most 3 an hour, 2 for any story (`desk_per_hour`), 1 only for Domains/Sport (`desk_beat_seat`), filled by a
  beat story first; with none ready it stays EMPTY (`desk.seats`; diagnostics `beat_seat`). (4) Specialist feeds
  (Moneycontrol, ET Markets, ESPNcricinfo ...) are ordinary feeds with `beat:`, added once embedding room is
  measured; section feeds, not everything-feeds. **Site:** a secondary with sections of its own opens them the way
  a primary does: Domains slides to the left as the back button (‹ Domains) and its five take the other places;
  a domain filters, tapped again = all of Domains; ‹ Domains = back to Business (`sub`, `ter` in index.html).
  Trade-press exclusives (one outlet) are not published: the same rule a niche political story faces.
- **Outlets outside India (owner, Oct 9 2026; `worldgate.py`, `priority.py`, `config/ownership.yaml`).** Two jobs:
  India's stories seen from outside (independent origins, real cross-border disputes) and world stories of global
  impact. Indian coverage decides relevance: (1) a world outlet's article joins stories Indian outlets cover;
  (2) Indian outlets' world pages are read too; (3) a story no Indian outlet covers is a candidate only with 3+
  independent world outlets AND two "yes" to one global-impact question (asked twice, reverse order; unsure = no;
  `analysis.world`). Ownership groups carry `region: world` (BBC included) and, for state media, `government:`
  and `voice:`. **State media** (the government controls the editorial line, no legal guarantee of independence):
  their group key is `gov:<government>` (`wire.independence_groups`; a paper carrying "(Xinhua)" copy joins it),
  one origin with that government's officials, NEVER counted as an independent outlet (`wire.independent`:
  reading threshold, rating, origins `outlets`, `n_sources`, perspectives) and never a perspective unit. This
  also stopped PIB counting as an outlet. A statement only foreign state media report in their own voice gets the
  speaker "Chinese state media" etc. (`compose._state_voice`). Owner's borderline calls: Al Jazeera, The National,
  CNA = state; Arab News, DD News / AIR = not. Public broadcasters independent by law (BBC, DW, France 24, NPR)
  are ordinary outlets. Paywalled papers are not fed (option B). **Embeddings** (3,000 texts/day; 2,236-2,975
  used Oct 5-8 before this): a world-feed article is embedded only if it names India, shares 2+ names with an
  Indian headline of the last 48 h, or with headlines of 2+ other independent world outlets; the rest wait and
  are checked each run (`worldgate.hold`, code only). When the budget is short, outlets whose articles end in
  3+ outlet stories go first (`stories._yields`; Aaj Tak home 30%, The Hindu 23%, NDTV India 26%); nothing is
  dropped. An outlet in no group (found by search) is Indian unless its domain is another country's.
- **Scheduling:** Supabase `pg_cron` calls GitHub's workflow_dispatch at :05 every hour
  (`public.dispatch_pipeline()`, token in Vault `github_dispatch_token`); the pipeline has no GitHub
  schedule (removed Oct 8 2026). Since Oct 10 2026 the desk runs in the same job right after the pipeline
  (see "The newsroom lives in the job"); `writer.yml` is gone and pg_cron's desk job (`dispatch_desk`) is off.
- **The newsroom lives in the job (owner, Oct 10 2026: "we have no other option, go for it").** Supabase's free
  egress (5 GB/month) was used ~15 GB in 9 days, ~all of it the pipeline reading its own data back, and the org
  went into its grace period (a transfer to a fresh org is refused during it). Now: every newsroom table (feeds,
  articles, stories, claims, canonical, story_pairs, source_clusters, published, translations, story_links,
  quota_usage) lives in a Postgres 17 SERVICE inside the hourly job (hourly.yml), loaded from the saved copy and
  saved again at the end (`.github/scripts/newsroom.sh`): pg_dump, checked (articles/stories never empty, the
  main tables present), AES-256 encrypted with the `NEWSROOM_KEY` secret (public repo, outlets' text), kept in the
  Actions cache (`newsroom-<run>`) AND force-pushed to the `newsroom` branch (90 MB parts); restore takes the copy
  with the newer stamp and REFUSES to start with none (never an empty newsroom saved over the real one); save
  refuses unless this job loaded a copy. Supabase keeps the READER tables and the run logs (`db.READER_TABLES`:
  profiles, follows, push_subscriptions, notifications, audio_requests, audio_files, recaps, videos, saved,
  notify_state, account_events, runs, diagnostics): `Store` sends each statement to the database its tables live
  in (`READERS_DATABASE_URL` = Supabase, `DATABASE_URL` = the newsroom; one statement may not mix them: tests run
  split, conftest `two_databases`). The live articles are copied to Supabase's `published` after each save
  (`mirror.py`, md5 per row, JSON copied as exact text; writing is free) for the site's fallback and the audio
  job, which still runs on Supabase alone. One job per hour: gate (Supabase `runs`) -> load -> pipeline (45 min
  budget) -> archive -> desk (20 min) -> save -> [only if saved] mirror, feed, notifications, audio check; a
  published article is shown only once the hour is saved, so a lost hour is re-done, never published twice.
  Tools (replay, modelcmp, probe, daily archive) load a read-only copy (`.github/actions/newsroom-load`).
  `newsroom-seed.yml` made the first copy from Supabase (refuses if one exists unless "replace"); Supabase's own
  newsroom tables are frozen since and must never be written again. Every load/save/refusal is logged in
  Supabase `diagnostics` kind 'newsroom' (`newsroomlog.py`). `heavy.py` is unused in the job (reads are free).

- **Reader features (owner, Oct 9 2026; built functional and plain, the owner designs them later; never ask him UI
  questions).** Guiding rule: personalise by TOPIC, never by viewpoint; perspective sorts were refused ("our project is
  exactly against this"). Supabase migration `20261009000200_readers.sql` (applied): profiles, follows, push_subscriptions,
  notifications, audio_requests, audio_files, recaps, videos, saved, notify_state, account_events; RLS = a reader's own
  rows; `request_audio()` RPC; Web Push key in Vault (`vapid_private_key`, read by `public.vapid_private_key()`, pipeline
  role only; public key in `site/features.js` and `notify.py`).
  - **Accounts:** edge function `supabase/functions/account` (verify_jwt off, checks tokens itself): sign up with name, email
    and password, log in with email and password (2.1; login ids and guests dropped); users made with the admin API, email
    confirmed, so no Auth settings changed. No password reset yet (needs custom SMTP).
  - **Follows are for notifications only** (owner): sections at any level, primary or secondary both count ("any");
    states (`places.py`: the section call names states, code keeps one only if the article names it or a city of it; "New
    Delhi" alone is not Delhi); people/bodies (`people.py`, code only, titles stripped); a single story (a follow-up
    published = notified); the daily recap. EVERY match is notified: no cap, no quiet hours (owner). `notify.py` runs at
    the end of every desk run (queue only), one row per reader+kind+ref, which is also the site's inbox; the hourly job SENDS
    the pushes in its own step after the feed is published, and the site asks for en.json/hi.json with the minute in the
    address (`?m=`) to step past GitHub's 5-minute cache (owner, Oct 10 2026: the home page showed an article 5 minutes
    after its notification). Coming back to the page after 2 minutes away reloads the cards.
  - **Daily brief** (owner, Oct 10 2026, option B; `recap.py`, called by `notify.make_recap` on the desk run from 23:00
    IST): the day's stories section by section (Politics, Justice, Business, World, Life, then "Also today"), ONE short
    sentence per story (its main section only; a thread told twice in the day once, by its newest), most outlets first.
    One page-tier call per section rewrites the stories' LEADS as brief sentences; code checks each against its lead
    (`recap.problem`: numbers incl. number words, names or their initials, "not", the speaker kept, "allegedly" kept, no
    hedge or cause word, no act verb the lead lacks, <= 40 words) and a refused or missing one is the lead's own first
    sentence. NO new colouring (owner: "just use the colour of the sentences used"): a brief sentence takes the weakest
    colour of the lead sentences it uses. Hindi: the brief's sentences through the translation cache
    (`compose.translate_strings`), else the Hindi lead's first sentence. No audio (owner, Oct 10 2026). Payload
    `{kind: "brief", sections: [{key, label, sents: [{t, c, id, p?}]}], stories}`; the site's Daily recap card shows it
    (a tap on a sentence opens its article; older recaps as a list).
  - **Audio on request** (`audio.py`, workflow `audio.yml`, started by the desk when something waits; GitHub schedule every
    3 h as backup): language chosen by the reader; 1 new request per reader per IST day, audio already made is free; every
    "tts" model on every key (10/day each); headline + article as written + section headings; one voice; the site lights
    each sentence in its own colour while it is read (timing file per audio; owner: highlighting only). Files on the
    `audio` branch (force-pushed, live articles and 7 days of recaps), published under /audio/ by audio.yml and site.yml.
  - **Videos** (`videos.py`, desk): once per new article, two YouTube searches (English and Hindi headline), YouTube's own
    order (owner), max 2 per channel, uploaded after the story's first report, "Primary footage" badge, "not checked"
    note. A video is kept only if its title shares two root words with the English or Hindi headline (`videos.relevant`;
    Oct 10 2026, first run: YouTube filled empty searches with audiobooks, Chinese dramas and Medicare ads). A video's
    language is read from its TITLE (`videos.title_lang`, site `vlang`: Devanagari or two romanised Hindi words = Hindi),
    not from the search that found it. The audio and video cards open complete: their data is fetched when the
    article opens (`prefetch`) and a tap waits for it before the card slides up (owner, Oct 10 2026). Needs the `YOUTUBE_API_KEY` secret (owner adds it after the design pass); without it nothing runs.
  - **On the site: trial_version_2.1 (owner, Oct 9 2026; designed on the canvas, approved, built into site/index.html).**
    A plain interface published earlier without his asking was taken back the same night: never publish site/UI changes
    he has not approved. 2.1: a 30px menu button (the reader's initial once signed in) at the LEFT of the wordmark; the
    language switch and the article's back button and switch the same 30px (touch area 7px wider, `.hit`). After 7 s or
    two swipes the greeting fades and the language switch moves up beside the wordmark (`tidy()`): the header's height
    changes ONCE; the indigo (`#hbg`) is clipped up and the current card's top edge (clip on its slot) and content
    (transform) glide. Never animate the header's or the cards' height (scroll-snap makes the card jitter). Menu: Sign in
    / name + email, Sort (client-side, not remembered), Following / Read later / Daily recap / Search ("Coming soon"),
    Log out. Sign in card: name, email, password (log in: email, password); the session stays on the device (`np-me`,
    refresh token) until log out. Article: the colour bar flies from the card to under the headline (`flyBar`), then the
    colour names and the audio and video buttons. Audio card: sign in first; Hindi | English; 1 new audio a day; make or
    play; a player strip lights each sentence (`.sent.lit`). Video card: YouTube's order, Hindi first in turn, links out
    (needs YOUTUBE_API_KEY).
    **2.2 menu (owner, Oct 10 2026, canvas, approved):** everything needs sign-in. Following = one card: each section
    row a circle (follow all of it; light green gradient when on) and an arrow (its sub-sections), multi-select;
    States; Words and names (filters became follows: a word follow notifies when an article contains the word,
    `notify.word_in` over headline + text, English and Hindi, whole words, or a person the article names). Notifications
    = "My audio" (article, language, status) + every notification, unread count on the menu button. Daily recap = made
    at 11 pm IST, countdown, Notify me toggle (follow `recap|daily`), after 11 pm the articles and Listen. Search and
    Read later removed ("a news app, not Instagram"). Sort and Log out stay. The phone's back closes any open card.
    Sign in / log in show a spinner ("Signing in…") then a tick and "Welcome, <name>" before the card closes (owner, Oct
    10 2026: it looked frozen). The article page never scrolls sideways (`.sheet-scroll` overflow-x hidden, pan-y).
    Not built: share card.
    Skipped by the owner: "what changed since you last read", corrections log, outlet pages, perspective sorts. Not
    features (pipeline discussion later): search grounding for sources, transcripts as evidence, Embedding 2.

## Pipeline (nishpaksh/run.py)
ingest RSS → proactive search (`discover.py`: Google News decoded, Bing; Tavily fallback) →
retract headline-only reads → Tavily reads blocked pages → wire copies → grouping (`stories.py`) →
read articles (`extract.py`, Flash-Lite) → per story: match statements → consolidate
(`consolidate.py`: merge duplicates, contradictions, one spelling per name, who says what) →
perspectives → origins + fact/characterisation → verdicts (`verify.py`) → settled? (`editions.py`) →
colours of published articles mature → retention → health checks in `runs.stats.health` → (workflow
step) articles older than 3 days to the archive branch. Rating (`priority.py`) comes after grouping;
search, Tavily reads, reading and analysis cover the preparation queue only.
Writing desk (`desk.py`, after the pipeline in the same job): settled stories, most important first → follow-up? → page, written once
(`compose.py`: threads, the news; `narrative.py`: the essay led by the news; `news.py`: headline) →
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
- **Flaky tests (seen Oct 8 2026, before the split work too):** `test_a_later_development_links_to_its_story_and_gets_background`
  and `test_published_article_is_closed_and_only_its_colours_mature` fail about 1 run in 3-8; they pass alone.
  Find the nondeterminism (set order, wall clock) before trusting a red run.
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
- Story 14231 (Oct 8 2026): one "What they say" paragraph of 18 lines, each "..., according to Modi".
  Code finds a paragraph naming one speaker in 3+ sentences or running 7+ sentences (`narrative._cohere`)
  and one writer call rewrites it (speaker named once, then "he said" only where the statements make the
  gender clear, else the surname; grouped into 2-3 paragraphs by subject); kept only if it passes every
  check and carries every statement, at most 2 per article. Code never writes a pronoun itself (it cannot
  know anyone's gender). Also: days and months are not names, and the numbers of a date are not figures
  (`relate.Profile`: "on Thursday, October 8, 2026" kept one call told twice apart); two lines with
  different years or dates are never one fact.
- Story 11867 (Oct 8 2026): (1) dispute marks made before the dispute gate were published, because the
  story was never reviewed again: the desk now runs `consolidate_story` before writing (a no-op when the
  review is current), so an article is written only from analysis under the current rules; (2) the
  validator rejects a sentence naming the same multi-word name twice ("repeats a name"; disputes exempt)
  and the fill pass rewrites it; the writer is told to use "the river", "he", "it" the second time.
- Story 12687 (Oct 7 2026): (1) `style.py` shortened "Commission for Air Quality Management" to
  "Commission for Management" ("Air" read as a title): only PERSON_TITLES count as evidence of a person,
  and institutional words (Management, Commission, Quality...) are never a person's name; (2) a page's text
  about its own publisher ("Hindustan was established in 1936 ...") is dropped by code
  (`compose.drop_outlet_self_talk`) and extraction is told to ignore it; (3) every article opens with a
  news lead: a missing "news" section triggers the news fill, and failing that the first "What happened"
  paragraph becomes the lead; (4) "By the numbers" takes figures only, never years or dates
  (`narrative._is_figure`); (5) a context line that shared no word with the story was dropped by code;
  replaced Oct 8 2026 by the model check (see "Context: better a line that makes a reader wonder"): words
  dropped real context. A page's text about its own publisher is still dropped (`drop_outlet_self_talk`).
- **Context: better a line that makes a reader wonder than a missing one (owner, Oct 8 2026; `belong.py`).**
  Story 12099: a Kerala vigilance probe under Related events in the cheetah story (a video page's list of
  other videos); on the 60 latest articles many context lines were other news from the page (a cricket
  comeback in a story of students' deaths). Owner's rule, for background, related and explanation alike:
  "It's better to have something unrelated in the story and think, why is this here, than not to have
  something important." So a line is dropped ONLY when the model says "other news" twice (one plain
  question, connected / other news, the story's news and own statements in front of it, worked examples;
  a line is asked again, lines reversed, only after one "other news"); one "connected", no answer (quota)
  or a model that never answers keeps it; the story never waits for this. Answers kept per line text in
  `stories.analysis.context_checks`. Reactions and "what next" are not asked. NO code rule drops a context
  line by its words (the no-shared-word rule dropped "Indian Air Force helicopters dropped rations in Saran"
  in a story on Saran's flood victims; a word rule dropped "the project is expected to generate employment"
  in the project's own story). Tried and rejected on 16 real stories the same night: per-role bars
  ("connected" twice for related and explanation, a named term for explanations): they lost the Prakash
  Singh rules in a DGP story when the model wavered once, and "Section 22" in a story on voter deletion.
  Reading is told to ignore other stories a page lists (`extract.py`). `replay --mode context` runs only
  this check on stored stories (unpaced: run it after the 12:30 IST reset, it spends the writer's quota).
- Story 16000 (Oct 9 2026): one hearing written three times although covered lines existed for exactly that.
  `perspectives.analyze_story` rewrote `stories.analysis` keeping a FIXED LIST of other fields: "consolidated" was
  on it, the review's results were not, so covered lines, updates, doubtful disputes and the cached same /
  conflict / topic answers were wiped every run, and the desk's own review saw the story as done. Doubtful
  disputes block green, so this could also show a statement as established. Now every stage's work survives
  (`{**old_analysis, ...own fields}`); `CONSOLIDATE_VERSION` 10 reviews every story again. Any stage writing
  `stories.analysis` must read the whole record and change only its own fields. The split moved to the
  analysis tier ("light"): the reading tier refused 14 of 15 calls in a night run and nothing was split.
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
- Story 13058 (Oct 9 2026): "Nana Patekar was an extraordinary artist ..." (Modi's words, read as his by reading)
  opened the article as a plain fact: a statement's speaker came only from the review model's list, which missed
  81 of the 627 lines on the 40 latest articles that every report gives to someone. Now, when every report of a
  line gives it to the same speaker (stance "attributes", one `attributed_to`), that is its speaker
  (`compose._reading_speaker`); an outlet, "media" or "reports" never is.
- Story 16197 (Oct 9 2026): the Hindi report names Chief Justice सूर्यकांत; reading wrote "Chief Justice Sanjiv
  Khanna (referred to as Chief Justice Suryakant in the text)", a name from the model's own out-of-date memory, and it
  was published. Reading now drops (never patches) a statement naming a titled person the report does not name in
  any spelling or script (`textmatch.absent_people`: names written in one rough Roman form with vowels, both scripts;
  same consonants or close spelling = present; a name split differently counts), or a note about the text itself
  (`extract.ground`, `extract.META`); the prompt says names, posts and numbers come only from the article. On 183
  real statements naming a titled person (Hindi and English) it flagged only that line.
- Story 15429 (Oct 8 2026): the page models were overloaded mid-translation and the Hindi page went live
  half in English. The desk now finishes half-translated Hindi pages after writing
  (`compose.finish_translations`, selected in SQL by `payload_hi.translation_complete = false`, up to 3 per
  run; cached strings cost nothing). Only the translation is completed; the English article stays closed.
- Run stats: `rated`, `queue`, `settled`, `colours_matured`, `live_pages`; the desk's own stats are in
  `diagnostics` (kind 'desk': ready, tried, published, tier_calls). Health flags a writer with 0
  successes in the desk's last 2 runs and two clock hours without an article while stories are ready
  (`run.writer_silent`). `published.updated_at` is the publication time.
- The news pick puts the newest day first and the past last (`news.pick_news`); sorted by support, old
  background outranked the new development and got tied to it with "after".
