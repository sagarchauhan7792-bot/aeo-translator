# Setup

Everything below is optional at install time. The pipeline runs without any of
it using `--engine mymemory --no-publish`; each missing credential produces a
message naming the file to create and where to get it, not a stack trace.

## 1. Python packages

```bash
pip install trafilatura beautifulsoup4 lxml regex indic-transliteration \
            google-api-python-client google-auth-oauthlib google-auth-httplib2
```

`google-ads` is only needed for keyword volumes:

```bash
pip install google-ads
```

## 2. Bhashini (translation) — free

The production translation engine. Registration is free.

1. Sign up at <https://bhashini.gov.in>
2. Go to **ULCA → My Profile → API Key**
3. Copy the **User ID** and the **ULCA API Key**

Then either set environment variables:

```bash
export BHASHINI_USER_ID=...
export BHASHINI_API_KEY=...
```

or save each value in its own file next to the code:

```
aeo-translator/bhashini_user_id.txt
aeo-translator/bhashini_api_key.txt
```

Check it works:

```bash
python -c "from translate import client; print(client().endpoint()[0])"
```

That prints the inference endpoint returned by the pipeline config call. The
config response is cached for six hours in `cache/pipeline_config.json`.

## 3. Google Docs + Sheets — for publishing

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   a project (or pick an existing one).
2. Enable three APIs: **Google Docs API**, **Google Sheets API**, **Google Drive
   API**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app**.
4. Download the JSON and save it as `aeo-translator/credentials.json`.

The first publish opens a browser once to authorise. The token is cached in
`token.json` and refreshed automatically after that.

Scopes requested: `documents`, `spreadsheets`, `drive.file`. `drive.file` only
grants access to files this tool creates — it cannot read the rest of the Drive.

Docs land in a Drive folder named **AEO Translations**, in a per-language
subfolder. The tracker sheet is created once and its id remembered in
`sheet_state.json`; to point at an existing sheet instead, set `google.sheet_id`
in `config.json`.

## 4. Google Ads Keyword Planner — for real search volumes

Without this, the `volume_in`, `competition` and `cpc_inr` columns stay empty.
They are never estimated.

Create `aeo-translator/google-ads.yaml`:

```yaml
developer_token: YOUR_DEV_TOKEN
client_id: YOUR_OAUTH_CLIENT_ID
client_secret: YOUR_OAUTH_SECRET
refresh_token: YOUR_REFRESH_TOKEN
login_customer_id: YOUR_MCC_ID_NO_DASHES
use_proto_plus: True
```

and put the account id in `google_ads_customer_id.txt` (or
`GOOGLE_ADS_CUSTOMER_ID`).

Keyword Planner returns **banded** volumes (1000–10000) unless the account has
active spend; accounts with spend return exact figures.

Before using a language's volumes the tool queries `language_constant` to
confirm the id really is that language. A wrong id would silently return another
language's numbers, which is worse than returning none — so on a mismatch it
skips that language rather than reporting them.

## 5. Gemini (optional) — for unattended runs

The default writer backend, `claude_local`, needs a Claude Code session open, so
it cannot run on a schedule. For scheduled runs, get a free key at
<https://aistudio.google.com/apikey> and set `GEMINI_API_KEY` or save it to
`gemini_api_key.txt`, then:

```bash
python run.py --url ... --writer gemini_free
```

The model id is discovered from the API rather than hard-coded, so a retired
model degrades to the next available flash model instead of failing every run.

---

## Per-client configuration

### `config.json`

- `site_profiles` — brand name, voice, YMYL flag, location and sitemap per
  domain. `_default` covers anything unmatched.
- `aeo.hreflang_base` — the site's base URL, used for canonical and hreflang.
- `thresholds` — the rewrite gate. `rewrite_trigger_ai_pct` is 20 rather than 30
  deliberately: a document at 25% would fail the 20% pass bar while never
  triggering a rewrite, so it would be marked failed without anyone trying to
  fix it. Anything above 30 still always rewrites.
- `score_weights` — must sum to 1.0; the code refuses to start otherwise. Re-run
  `calibrate.py` after changing these.

### `glossary.json`

- `never_translate` — brand and product names kept in Latin script.
- `transliterate_only` — rendered in the target script, never semantically
  translated. **Mutually exclusive with `never_translate`**; a term in both
  produces a self-contradictory writer prompt.
- `preferred` — per-language renderings that override the MT's choice.
- `medical_claim_guard` — claims that may never be introduced, and hedge markers
  that may never be deleted.

Numeric protections (dosages, prices, phone numbers, URLs) live in
`patterns.py`, in code rather than JSON, because escaping regexes through JSON
is a reliable source of silent bugs.

---

## Blog Studio

```bash
python -m studio                 # or blog.bat
python -m studio --port 9000     # if 8765 is taken (it also auto-picks the next free port)
```

The app reports on load which credentials it found and which stages are
therefore available. With the default `claude_local` writer it queues packets
and pauses at the Draft stage; a free Gemini key makes every stage run in one
click.

## Sharing Blog Studio with your team

Blog Studio is localhost-only by default and needs no login there. The moment it
listens on anything else it **refuses to start without a password** — it holds
live API keys, can spend your quota, crawls whatever it is pointed at, and can
write to your Drive.

### 1. Set the shared password

```bash
python -m studio --set-password
```

Stored in `auth.json` as a scrypt hash with a random salt, never in plain text.
That file is gitignored. Share the password with your team over something better
than email.

### 2. Get a public HTTPS address

```bash
python -m studio --share
```

Prints a public URL and keeps the app on your own machine — your files, caches
and API keys never leave it. Needs `cloudflared`:

```bash
winget install --id Cloudflare.cloudflared
```

The quick-tunnel address changes on every restart and dies when the process stops
or the machine sleeps. For a fixed address on a domain you own, `--domain` does
the whole named-tunnel setup for you -- tunnel creation, DNS routing, and running
it -- and is safe to run every time, not just the first:

```bash
python -m studio --domain studio.yourdomain.com
```

The domain's DNS has to be managed through the same Cloudflare account. The very
first run needs a one-time interactive login so cloudflared can act on your
account -- it will tell you to run `cloudflared tunnel login` and stop; do that,
then run the same `--domain` command again.

### Or just your own network

```bash
python -m studio --host 0.0.0.0
```

Reachable from other machines on the same Wi-Fi. Still requires the password.

### Running with no login at all

`--no-auth` disables the sign-in page entirely, including on a public
`--share`/`--domain` address. This is a deliberate opt-in, not a default: without
it, exposing the app beyond localhost with no password configured still refuses
to start. Only pass `--no-auth` if you specifically want anyone who finds the
URL to have full access -- your API quota, the crawler, and (once Google OAuth
is set up) Drive write access, with nothing standing in front of it.

```bash
python -m studio --domain studio.yourdomain.com --no-auth
```

### Running it on Render, so the laptop can be off

`--share`/`--domain` both die with the terminal that started them. To keep the
app up independently -- and to let anyone open it without installing anything --
deploy it: [render.yaml](render.yaml) already describes the whole service, so
Render configures itself from the repo.

**1. Deploy.** In the Render dashboard: **New + → Blueprint**, pick this repo,
apply. That is the whole deploy; the service starts with no credentials and the
audit, ideas, site crawl and compare tabs already work.

**2. Decide what the public instance is allowed to do.** It runs **open** -- no
sign-in, by design, because a page nobody can get into is not public. Whoever
finds the URL can therefore use whatever you mount on it. Add Secret Files under
**Environment → Secret Files**, using exactly these names, and add only the ones
whose public use you accept:

| Secret File | Unlocks | Open to everyone means |
|---|---|---|
| *(none)* | audit, ideas, site crawl, compare | nothing of yours is spent |
| `gemini_api_key.txt` | Draft, Fix-it, the rewrite loop | strangers spend your free Gemini quota |
| `pagespeed_api_key.txt` | Core Web Vitals in the audit | strangers spend your PageSpeed quota |
| `bhashini_*.txt` | Bhashini translation | strangers spend your ULCA quota |
| `credentials.json` + `token.json` | Docs + tracker Sheet publishing | **strangers create Docs in your Drive** |
| `google-ads.yaml` + `google_ads_customer_id.txt` | keyword volumes | strangers spend your Ads API quota |

The last row is the one to think hardest about. Keep publishing on the copy you
run locally, where it is behind your own machine, and leave it off the public
one.

**3. Authorise Google locally first, if you do mount it.** The OAuth consent
screen needs a browser and the container has none, so run a publishing command
on your own machine, complete the browser step once, and upload the `token.json`
it writes alongside `credentials.json`. Without this the app says what is
missing rather than hanging.

**To put a password back on it:** set `OPEN=0` under **Environment**, and upload
an `auth.json` Secret File (from `python -m studio --set-password` locally).
Without that file it refuses to start, which is the interlock working.

**What the free plan costs you.** No persistent disk: `cache/`, `out/`,
`drafts/`, `reports/`, `packets/` and `state.jsonl` are wiped on every deploy and
every spin-down, so the Library tab starts empty again. Published Docs and the
tracker Sheet survive -- they live in your Drive. And the container sleeps after
about 15 minutes idle, so the next visit waits roughly a minute for it to wake.
A Render Disk and a paid plan fix both.

### What everyone shares

One workspace. Everyone who signs in sees the same crawls, drafts, reports and
library, and spends the same API quota. Sign-in asks for a name so jobs are
labelled with who ran them, but it is **not** per-user accounts and there is no
data separation. That is the intended design for a small team; do not hand the
URL to clients.

Jobs run one at a time, so two people clicking at once will queue rather than
collide.

### What protects it

| | |
|---|---|
| Password | scrypt hash, random salt, minimum 8 characters |
| Session | signed cookie, HttpOnly, SameSite=Lax, Secure behind the tunnel, 14 days |
| Brute force | 6 attempts per IP, then a 5-minute lockout |
| CSRF | cross-origin POSTs refused |
| Interlock | non-local bind is refused outright until a password exists |

Sign out at `/logout`.

## Verifying an install

```bash
python calibrate.py --fetch --build-mt-free   # fetch fixtures (network)
python calibrate.py                           # expect AUC 1.00 on both classes
python test_rewrite_loop.py                   # expect ALL CHECKS PASSED
python features.py                            # raw feature table by class
python run.py --file testdata/sample-post.md --langs hi --no-publish --engine mymemory
```

The last command pauses and queues a work packet. Run `/aeo-rewrite` and re-run
it to finish.

## Adding a language

1. Add an entry to `config.json → languages` (code, name, native, Bhashini code,
   script, Unicode range, region, honorific).
2. Add its rules to `linguistics.py`: `VERB_ENDINGS`, `AI_CONNECTIVES`,
   `CALQUES`, `HONORIFICS`, `FORMAL_MARKERS`.
3. Add question stems to `keywords.py → QUESTION_STEMS` and a Google Ads
   language constant to `LANG_CONSTANTS`.
4. Add native and MT fixtures under `samples/` and re-run `calibrate.py`. The
   bands in `quality.py` were fitted on Hindi; do not assume they transfer
   without checking.
