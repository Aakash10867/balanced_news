# Nishpaksh · निष्पक्ष

Indian news, separated into **what is established**, **what is contested**, and **how each side worded it**, refreshed every hour, readable in English or Hindi.

No outlet is labelled left or right by hand. Perspectives emerge from which sources agree with which, story after story.

## What a story page shows

One readable essay, every sentence coloured by how well it is supported, with numbered sources.

| Colour | Meaning | Who decides |
|---|---|---|
| Green: established | 3+ independent outlets (owners merged, wire copies merged) that were actually read report it; it traces to 2+ **independent origins**; nobody denies it; it is a checkable fact, not a characterisation; and it has stood 6 hours since all of that first held. Once perspectives exist, it must also be reported across 2+ of them. | Code |
| Grey, dotted: developing | Meets the green rule but has not stood 6 hours yet | Code |
| Grey: not yet cross-checked | Anything below the green bar, always written with attribution ("X reported that…") | Code |
| Amber: sources disagree | Someone reports it and someone denies or contradicts it | Code; contradictions found by a model |
| Red, tagged FALSE | Primary evidence (FIR, court record, official data, video) shows it is false | Two model families must agree; grounded search |

**Independent origins** (nishpaksh/origins.py): who actually knew the fact by their own means.
Ten outlets repeating one police statement have one origin, the police. Officials, ministries and
police of one government are one origin. A wire copy's origin is the agency. An outlet's own voice
counts only as original reporting (named reporter plus a dateline from the place, or details it
reported first that nobody else has); otherwise it, and every "sources said", goes to a shared pool
that never counts. Every uncertain case resolves toward fewer origins.

**Publishing.** A story is published when two perspectives cover it, or (interim rule, until the
system has learned perspectives) when 3+ independent outlets have been read and the story traces
to 2+ independent origins. The page says so. Outlets we could find but not read are listed as
"could not be read" and are never used for facts.

## How it works

```
RSS feeds (English + Hindi)                          plain HTTP, no AI
  → proactive search: who else covered our stories?  Google News / Bing (free), Tavily as fallback
  → blocked pages read through Tavily                ≤31 credits a day, booked before each call
  → wire-copy detection (MinHash + agency bylines)   code
  → group into stories (title + lead)                one Gemini embedding model; borderline → Flash-Lite
  → pick what to read: ≥2 independent sources, one article per owner, ≤8 per story
  → extraction: events, claims, times, loaded words  Flash-Lite; Gemma 4 as overflow
  → match statements across articles                 code; ambiguous pairs → Flash-Lite
  → perspectives from agreement                      code (signed graph + spectral clustering)
  → independent origins, fact vs characterisation    code; source names merged by Flash-Lite
  → verdicts                                         code; contested ones → search grounding
                                                     + Gemini Flash + Gemma must agree
  → timeline, essay, headline, Hindi                 code + Flash-Lite / Flash
```

Design rules worth knowing before you change anything:

- **The AI never decides structure.** Order, emphasis and verdicts are computed. The models only transcribe, match, check and translate.
- **When unsure, keep apart and count less.** Two events wrongly merged into one story mix their facts, so grouping prefers splitting. Two sources wrongly counted as independent make a rumour look established, so origins prefer merging.
- **Copies count once.** Wire copies, same outlet and same owner (config/ownership.yaml, public corporate facts) form one independent source.
- **"False" has a high bar.** Primary evidence *and* two model families agreeing. Official statements alone never suffice.
- **Perspectives are emergent.** Per story, sources split only if they directly contradict each other. Across stories, agreements build a source-to-source matrix clustered into stable perspectives (A, B, C…).
- **One embedding model.** Vectors from two models are not comparable; every stored vector is tagged with its model and re-made if the model changes.

## Free-tier budget (per day)

Every Gemini key (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, …) is a separate project with its own quota;
the router treats each key and model pair as its own budget and drops a key Google rejects.

| Tier | Models | Used for |
|---|---|---|
| bulk | 3.5 & 3.1 Flash-Lite, then Gemma 4 | reading articles |
| second | Gemma 4 31B, 26B | independent second opinion on verdicts |
| light | 3.5 & 3.1 Flash-Lite | matching, same-event checks, source names, headlines, translation |
| writer / judge | 3.x Flash | the essay; verdicts on contested claims |
| grounded | 2.5 Flash, 2.5 Flash-Lite | web search for primary evidence (~40/day) |
| embed | one Gemini embedding model | story grouping |

Tavily: 1,000 credits a month, at most 31 a day (unspent credits roll forward within the month):
about 5 blocked pages read per run (5 pages = 1 credit) and 1 search per run when the free searches
find nothing. Quotas reset at midnight Pacific (12:30 pm IST).

## Setup (about 20 minutes)

1. **Gemini key.** Create one at aistudio.google.com. Then check that the model names resolve:
   ```bash
   pip install -r requirements.txt
   GEMINI_API_KEY=... python -m nishpaksh.tools.list_models
   ```
   Anything marked DISABLED is simply skipped. Fix its ID in `config/models.yaml` if you want it back.
2. **Feeds.** `python -m nishpaksh.tools.check_feeds` lists which RSS URLs work. Fix or delete the failures in `config/feeds.yaml`. Feeds that fail for a full day are disabled automatically anyway.
3. **Database.** Create a free Supabase project and apply `supabase/migrations/*.sql` in order (the
   pipeline's own role cannot change the schema). Copy *Connect → Session pooler* connection string.
4. **GitHub.** Push this folder to a **public** repo. Under *Settings → Secrets and variables → Actions*,
   add `GEMINI_API_KEY` (optionally `GEMINI_API_KEY_2`), `TAVILY_API_KEY` and `DATABASE_URL`.
5. **Schedule.** GitHub's own schedule skips hours, so Supabase starts the run every hour at :05
   (`pg_cron`, see the scheduler migration). Create a fine-grained GitHub token for this repo only with
   *Actions: read and write* and store it in Supabase: `select vault.create_secret('<token>', 'github_dispatch_token');`
6. **Website.** `site/` (nishpaksh_version_1.1: one card at a time, the card grows into the article) is deployed to GitHub Pages by the `site` workflow. It reads the ready-made files on the `feed` branch (`nishpaksh/feed.py`), then Supabase, then the `archive` branch. Add `?lang=hi` for Hindi.

## Archive

Supabase keeps a rolling window (see `nishpaksh/retention.py`). Nothing learned is lost: the
`daily-archive` workflow exports each day, once it is 2 days old, to a gzipped JSON-lines file
attached to that month's GitHub release (`archive-YYYY-MM`). It holds articles' metadata, every
extracted statement, verdicts, perspectives and the published pages in both languages.
It deliberately excludes article bodies: the repository is public, and the text belongs to the
publishers. Each record keeps the article URL instead.

## Local run

```bash
pip install -r requirements.txt
GEMINI_API_KEY=... python -m nishpaksh.run          # uses data/nishpaksh.db (SQLite)
python -m pytest -q                                 # no API key needed
python -m nishpaksh.tools.probe --parts search      # measurements on real data → diagnostics table
```

## Tuning

All thresholds are in `nishpaksh/config.py`. Watch these first on real data:

- `story_join_cosine`, `story_core_cosine`, `story_ask_cosine`: calibrated on real article pairs labelled
  same/related/different (`tools/probe.py --parts grouping,embedcmp`). Re-measure before changing them.
- `established_*`: the green rule. Every run's record (`runs.stats.health`) reports any established
  statement that does not meet it, the largest stories, and failed steps.
- `claim_same_cosine` / `claim_candidate_cosine`: the auto-merge and send-to-LLM bands for statement matching.
- `split_margin`: how much more sources must agree within a side than across sides.

## Known limits

- The two verdict models are both from Google, so their training biases are correlated. Adding a model from another provider as the second opinion would make the check more independent.
- Hindi articles are reduced to English statements internally, so the Hindi page is a translation of those statements, not the original Hindi wording. The loaded words are kept exactly as the original source wrote them.
- Pages we cannot read even through Tavily are listed as coverage only; their facts are never used.
- If no outlet reports a fact, we cannot know it exists: the site is bounded by what Indian media publishes.
- Two outlets quietly relying on the same off-record source cannot be told apart from independent reporting.
- Grounded search returns redirect URLs from Google. Displayed evidence links may pass through a Google redirect.
