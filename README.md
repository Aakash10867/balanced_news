# Nishpaksh · निष्पक्ष

Indian news, separated into **what is established**, **what is contested**, and **how each side worded it**, refreshed every hour, readable in English or Hindi.

No outlet is labelled left or right by hand. Perspectives emerge from which sources agree with which, story after story.

## What a story page shows

| Section | What goes in it | Who decides |
|---|---|---|
| What happened | Events reported by ≥2 independent sources from ≥2 perspectives, in time order. Events whose order is not established share a tier. | Code |
| Established facts | Non-event statements meeting the same bar | Code |
| Contested and unverified | Everything else, each tagged **Disputed**, **Unverified**, **False** or **Confirmed**. Nothing is dropped; single-article details sit in a collapsible list. | Code; False/Confirmed need two AI models agreeing on primary evidence |
| How each side worded it | The emotive words each perspective used for the same fact | Recorded by AI, grouped by code |
| Sources | Every article read, with its perspective | Code |

A story is published only if at least two perspectives cover it. One-sided stories are still read and stored, because they teach the system who agrees with whom.

## How it works

```
RSS feeds (35 outlets, English + Hindi)          plain HTTP, no AI
  → wire-copy detection (MinHash + agency bylines) code
  → extraction: events, claims, times, loaded words  Gemma 4 31B / 26B
  → group into stories                             Gemini Embedding, TF-IDF fallback
  → match statements across articles               code; ambiguous pairs → Flash-Lite
  → perspectives from agreement                    code (signed graph + spectral clustering)
  → verdicts                                       code; contested ones → search grounding
                                                   + Gemini Flash + Gemma must agree
  → timeline (partial order over time intervals)   code
  → headline, Hindi translation                    Flash-Lite
```

Design rules worth knowing before you change anything:

- **The AI never decides structure.** Order, emphasis and section placement are computed. The models only transcribe, match, check and translate.
- **Copies count once.** PTI/ANI/Bhasha copy and same-outlet articles form one independent source.
- **"False" has a high bar.** It requires primary evidence (FIR, court record, official data, video) *and* two model families agreeing. Official statements alone never suffice. When quota runs out, claims stay "unverified" rather than being guessed. The design accepts missing a false claim in exchange for almost never calling a true claim false.
- **Perspectives are emergent.** Per story, sources split only if they directly contradict each other on a fact. Across stories, the per-story agreements build a source-to-source matrix that is clustered into stable perspectives (A, B, C…). Until there is enough history, splits are per story and marked with `*`.
- **The headline is checked.** It is generated from established facts only and rejected if it contains any loaded word any source used.

## Free-tier budget (per day)

| Tier | Models | Requests/day | Used for |
|---|---|---|---|
| bulk | Gemma 4 31B, 26B | 28,800 (TPM-bound to roughly 8,000 articles) | reading every article; second opinion on verdicts |
| light | 3.5 & 3.1 Flash-Lite | 1,000 | matching, headlines, translation |
| judge | 3, 3.5, 3.6, 3.7, 3.8 Flash, Robotics ER 2 | 120 | verdicts on contested claims |
| grounded | 2.5 Flash, 2.5 Flash-Lite | 40 | web search for primary evidence |
| embed | Gemini Embedding 2, 1 | 2,000 | story grouping |

The router spreads each day's remaining quota over the hourly runs left, falls back across models in a tier, and remembers usage in the database. Quotas reset at midnight Pacific (12:30 pm IST).

## Setup (about 20 minutes)

1. **Gemini key.** Create one at aistudio.google.com. Then check that the model names resolve:
   ```bash
   pip install -r requirements.txt
   GEMINI_API_KEY=... python -m nishpaksh.tools.list_models
   ```
   Anything marked DISABLED is simply skipped. Fix its ID in `config/models.yaml` if you want it back.
2. **Feeds.** `python -m nishpaksh.tools.check_feeds` lists which RSS URLs work. Fix or delete the failures in `config/feeds.yaml`. Feeds that fail for a full day are disabled automatically anyway.
3. **Database.** Create a free Supabase project. Copy *Connect → Session pooler* connection string. Tables are created on the first run.
4. **GitHub.** Push this folder to a **public** repo, since hourly runs are free only on public repos. Under *Settings → Secrets and variables → Actions*, add `GEMINI_API_KEY` and `DATABASE_URL`. Run *Actions → hourly-pipeline → Run workflow* once by hand to check.
5. **Website.** On share.streamlit.io, deploy `app/streamlit_app.py` from the repo. Under *Secrets*, add `DATABASE_URL` (see `.streamlit/secrets.toml.example`).

Share `…streamlit.app/?lang=hi` to open it in Hindi.

## Local run

```bash
pip install -r requirements.txt
GEMINI_API_KEY=... python -m nishpaksh.run          # uses data/nishpaksh.db (SQLite)
streamlit run app/streamlit_app.py
python -m pytest -q                                 # 13 tests, no API key needed
```

## Tuning

All thresholds are in `nishpaksh/config.py`. Watch these first on real data:

- `story_join_cosine_embed`: lower it if one event splits into several stories; raise it if separate events merge.
- `claim_same_cosine` / `claim_candidate_cosine`: the auto-merge and send-to-LLM bands for statement matching.
- `split_margin`: how much more sources must agree within a side than across sides.

## Known limits

- The two verdict models are both from Google, so their training biases are correlated. Adding a model from another provider as the second opinion would make the check more independent.
- Hindi articles are reduced to English statements internally, so the Hindi page is a translation of those statements, not the original Hindi wording. The loaded words are kept exactly as the original source wrote them.
- Paywalled or JavaScript-only pages fall back to the RSS summary, which carries less detail.
- Grounded search returns redirect URLs from Google. Displayed evidence links may pass through a Google redirect.
