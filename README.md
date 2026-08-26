# aeo-translator

Translate a blog post into eight Indian languages plus Hinglish, restructure each
one for answer engines, score it, rewrite anything that reads like machine
output, and publish it as a Google Doc with a row in a tracker sheet.

Built for regional-language AEO/GEO work on Indian client sites.

```
ingest → translate → transcreate + AEO → score → [rewrite loop] → publish → log
```

Languages: Hindi, Marathi, Gujarati, Bengali, Tamil, Telugu, Kannada, Punjabi,
and Hinglish (Roman-script Hindi — how most tier-2/3 users actually type a
search query).

---

## The two things you should know before using it

### 1. The "AI %" is a proxy, and it says so everywhere

No AI detector is validated on Hindi, Marathi, Gujarati, Bengali, Tamil, Telugu,
Kannada or Punjabi. GPTZero and Originality.ai are English-first and their Indic
scores are unaudited. Reporting one of those numbers on Devanagari would be
reporting noise with a decimal point on it.

So this does not wrap a detector. It measures six things that are real and
observable in Indic text and reports the inverse of the composite as
**`AI-likeness % (proxy)`** — the label used in the sheet, the report and the
Doc, so it can never be mistaken for a detector reading.

### 2. Bhashini cannot do the rewrite loop

[Bhashini](https://bhashini.gov.in) is the right translation engine for these
languages — free, government-run, purpose-built. But it is a *translation* API.
It cannot rewrite for tone, restructure for answer engines, or act on feedback.

So the two jobs are split:

| Job | Engine |
|---|---|
| Translation + back-translation | Bhashini |
| Transcreation, humanising, AEO restructuring, rewrite loop, review | a writer backend |

Writer backends, tried in order: `claude_local` (work packets processed by a
`/aeo-rewrite` slash command inside Claude Code — free, no API key, but needs a
session open, so it is refused on a deployed host where nothing can answer it),
`gemini_free` (Google AI Studio free tier — needed for unattended runs), then
`openai_compat` (any OpenAI-shaped endpoint — somewhere to go when Gemini's free
quota runs out mid-run).

**Until Bhashini credentials exist, Gemini does the translating** — and it is
asked for the target language's register rather than a literal rendering, using
the same measured rubric the transcreation pass is held to. An NMT service can
only translate, so tone has to be repaired afterwards; a model that can write
can be asked for it the first time. The English back-translation deliberately
stays literal: it is the fidelity measuring instrument, not a deliverable.

---

## The scorer, and why it is calibrated

Six sub-scores combine into a Human-Likeness Score; `AI-likeness % = 100 − HLS`.

| Sub-score | Measures |
|---|---|
| fidelity | back-translation round trip, number/dosage integrity, locked terms, medical-claim inflation |
| grammar | script purity, spaced punctuation, detokenisation artefacts |
| translationese | prepositional calques, English-shaped relative clauses, comma density |
| register | honorific consistency, over-Sanskritised vocabulary |
| burstiness | sentence-length variation, paragraph uniformity, repeated openings |
| native_review | an independent reviewer pass, given only the target text |

`calibrate.py` tests whether those numbers mean anything, against three classes
of real text:

```
native      BBC Hindi journalism           HLS 92.3   AI-likeness  7.7%
translated  Hindi Wikipedia (from English) HLS 74.7   AI-likeness 25.3%
mt          raw machine translation        HLS 56.0   AI-likeness 44.0%

native vs translated   AUC 1.00   Cohen's d 3.19
native vs mt           AUC 1.00   Cohen's d 3.25
```

**Running this is what made the scorer work.** The first version scored AUC 0.21
— it ranked translated text as *more* human than native text. Calibration
falsified three assumptions that all sounded obviously correct:

- *"Devanagari prose ends sentences with a danda; a full stop reads as machine
  output."* BBC Hindi, written by Hindi journalists, uses a danda in **0%** of
  paragraphs. Both negative classes used one in ~98%. The rule was backwards and
  was penalising the most native text in the sample. Now measured, not scored.
- *"Native prose has shorter sentences than translated prose."* It is longer:
  22.5 words against 19.4.
- *"Verb-finality is the dominant translationese signal."* Native 0.921,
  human-translated 0.956, raw MT 0.906. Indic NMT gets word order right. This was
  the headline hypothesis of the design and it carries no weight in the score.

What survived, measured per 1000 words (native vs raw MT): space before
punctuation 0.5 vs 29.8 · prepositional calques 0.4 vs 10.7 · English-shaped
relative clauses 0.4 vs 3.7 · comma density 22 vs 72 · sentence-length CV
0.58 vs 0.47.

`python features.py` prints the full table. Change a rule, re-run
`calibrate.py`, look at the separation before trusting it.

---

## Blog Studio — the daily app

A locally-run browser app over the same pipeline, covering the whole blogging
day rather than just the translation step:

```
idea  ->  draft (English, AEO-structured)  ->  translate  ->  Google Doc + sheet
```

```bash
python -m studio          # or double-click blog.bat
```

Opens on http://127.0.0.1:8765. Eight tabs:

- **My Blog** — the front door. Paste a post, fetch a URL, or pick a draft, and
  get a full on-page SEO audit: title and meta length with a real SERP preview,
  heading structure, keyword density and placement, Flesch readability, sentence
  and paragraph rhythm, filler and generated-text phrases, internal and external
  links with live broken-link checking, image alt coverage, slug quality,
  structured data, answer-engine readiness, and E-E-A-T including the YMYL rules
  for health content. Every finding comes with its specific fix, and the
  JSON-LD it generates is ready to paste into your page head. Needs no
  credentials.
- **Ideas** — expands a topic through Google autocomplete, then checks every
  candidate against the site's own sitemap and flags it *covered* / *partial* /
  *gap*. On a site with a couple of thousand posts, "have we already covered
  this?" matters more than search volume, and writing the same post twice splits
  its own rankings. People Also Ask is deliberately not used: google.com/search
  returns a JavaScript-only shell to a plain HTTP client, with no question data
  in it at all.
- **Draft** — brief to an English post written answer-engine-first. Every figure
  it writes is surfaced for you to verify, because a draft has no source
  document and therefore nothing the fidelity check can compare against.
- **Translate** — the pipeline below, unchanged.
- **Site** — crawls the whole site for what only exists between pages: duplicate
  titles, cannibalisation, thin content, orphans, and specific internal links to
  add with the anchor text to use. Throttled, robots-aware, resumable.
- **Compare** — diffs your page against competitors and returns a content-gap
  list rather than a score.
- **Report** — one self-contained HTML file for a client, print-clean to PDF,
  plus a CSV of every finding.
- **Library** — everything processed, with scores and Doc links.

### Sharing it with a team

```bash
python -m studio --set-password    # once
python -m studio --share           # public HTTPS via Cloudflare Tunnel
```

The app stays on your machine; Cloudflare provides the address. It **refuses to
listen on any non-local address until a password is set**, because it holds live
API keys and can write to your Drive. One shared password, one shared workspace —
not per-user accounts. See [SETUP.md](SETUP.md).

### The page on GitHub Pages

`index.html` sits at the repo root and is the whole front end -- one file, no
build step. It is served two ways from that single copy:

- by the app itself, at `http://127.0.0.1:8765`, where the API is same-origin
  and nothing has to be configured;
- as a static page at
  [sagarchauhan7792-bot.github.io/aeo-translator](https://sagarchauhan7792-bot.github.io/aeo-translator/),
  where there is no backend underneath it at all.

The hosted copy is the interface, not the tool: the crawler, the scorer, the
translator and the credentials all live in the Python app. So it opens asking
for the address of a running backend rather than pretending to work, and
remembers it (`?api=http://127.0.0.1:8765`, kept in `localStorage`).

Driving a local backend from that hosted page is cross-origin, which the API
refuses by default. Allow it explicitly, per origin:

```bash
python -m studio --allow-origin https://sagarchauhan7792-bot.github.io
```

Without that flag nothing changes: same-origin only, exactly as before.

What the audit deliberately does **not** report: search volume, keyword
difficulty, backlinks and rank tracking. None of those can be measured from the
page itself. Volume wires into Google Ads when the credentials exist; the rest
need a paid data provider, and inventing them would be worse than their absence.

The English draft is scored on **AEO + rhythm**, not AI-likeness. Three of the
six sub-scores below measure Indic-specific phenomena and are undefined on
English, and the calibration behind the proxy was measured on Hindi. The app
says so on screen rather than reusing a number outside the range it was
measured in.

## Quickstart

```bash
pip install trafilatura beautifulsoup4 lxml regex indic-transliteration \
            google-api-python-client google-auth-oauthlib google-auth-httplib2

# one live post, Hindi, local files only
python run.py --url https://example.com/blog/post/ --langs hi --no-publish

# a whole sitemap, five posts, three languages, published to Docs + Sheet
python run.py --sitemap example.com --filter /blog --limit 5 --langs hi,mr,ta

# a manually supplied post, every language
python run.py --file mypost.md --langs all
```

With the `claude_local` backend the run pauses and queues work packets. Run
`/aeo-rewrite`, then re-run the same command — translations are cached and the
ledger resumes exactly where it stopped.

Verify the scorer before trusting it:

```bash
python calibrate.py --fetch --build-mt-free   # build fixtures
python calibrate.py                           # separation report
python test_rewrite_loop.py                   # loop control-flow tests
```

---

## What the AEO layer adds

- **TL;DR block** — the passage answer engines lift first
- **Question-form headings**, each with a 40–60 word direct answer that stands
  alone when quoted
- **6–10 FAQs** built from real regional keyword shapes
- **JSON-LD**: `Article`/`MedicalWebPage` + `FAQPage` + `BreadcrumbList` +
  publisher, with `inLanguage`, `speakable` and entity anchors
- **hreflang cluster** across all variants plus `x-default`
- **Transliterated Roman slugs** — `/hi/diabetes-ke-shuruati-lakshan/`, not a
  percent-encoded Devanagari URL
- **Entity anchoring** — named entities get cited; pronouns do not
- **YMYL guard** — author and reviewer credentials in schema for health content

## Safety rails

Enforced in code, not trusted to the model:

- Every number in the source must appear in the translation. A 500 mg dose
  quietly becoming 50 mg **blocks publication**.
- Locked brand and product terms must survive.
- No medical claim may come out stronger than it went in, and a hedge present in
  the source may not be deleted.
- A rewrite pass that improves style while breaking a fact is rejected and the
  previous version is kept.
- A document that cannot reach the threshold is marked `NEEDS_HUMAN_REVIEW` and
  flagged red in the sheet, never silently published.
- Keyword volumes are left **empty** when Google Ads credentials are absent.
  Nothing is estimated — a fabricated search volume in a client deliverable is
  indistinguishable from a measured one.

## Credentials

All optional at build time, required at run time. Each failure names the file to
create and where to get it, rather than raising a stack trace. See
[SETUP.md](SETUP.md).

| For | Needs |
|---|---|
| Translation | Bhashini ULCA user id + API key (free) |
| Docs + Sheets | Google OAuth `credentials.json` |
| Keyword volumes | `google-ads.yaml` + customer id |
| Unattended runs | Gemini AI Studio key (free tier) |

`--engine mymemory` runs the whole pipeline with no credentials at all, using
MyMemory's free public API. It is rate-limited and lower quality — a way to
verify the pipeline, not to ship client work.

## Layout

```
index.html       the Blog Studio front end -- one file, no build step
run.py           orchestrator, rewrite loop, resume
sources.py       file | url | sitemap ingest
extract.py       HTML → Article model
translate.py     Bhashini ULCA client + cache (+ MyMemory test engine)
transcreate      writer/base.py holds every prompt
aeo.py           TL;DR, answers, FAQ, JSON-LD, hreflang, slugs, rendering
quality.py       six sub-scores → HLS → AI-likeness % (proxy)
features.py      raw measurement, separate from scoring, inspectable
linguistics.py   per-language rules, including the ones that failed
calibrate.py     does the scorer separate native from translated?
keywords.py      regional keyword sets + Google Ads volumes
publish_gdocs.py formatted Google Docs
sheet.py         the tracker sheet
studio/          the local server behind index.html
docs/            the project page published at /docs/
```

## Licence

MIT.
