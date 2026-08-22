"""Regional keyword sets for the AEO layer, with real volumes when available.

Two halves, deliberately separable:

  for_article()   Builds the seed keyword set from the article itself. Always
                  works, no credentials, no network.
  fill_volumes()  Attaches real avg monthly searches / competition / CPC from
                  Google Ads Keyword Planner. Needs google-ads.yaml.

When the credentials are missing the volume columns stay EMPTY. They are never
estimated. A fabricated search volume in a client SEO deliverable gets acted on
as if it were measured, and there is no way for the client to tell it apart from
a real one -- which makes guessing worse than leaving the cell blank.

The Google Ads call reuses the request shape already proven in
C:/Claude/hiims-seo/fill_volumes.py.
"""
from __future__ import annotations

import csv
import datetime
import re
from pathlib import Path

from common import ROOT, MissingCredential, lang_by_code, log, warn
from extract import Article

YAML_PATH = ROOT / "google-ads.yaml"
CACHE = ROOT / "cache" / "keywords"

GEO_INDIA = "2356"

# Google Ads languageConstant ids. Verified against the API before use by
# _verify_language(): a wrong id would silently return volumes for a different
# language, which is worse than returning none.
LANG_CONSTANTS = {
    "en": "1000", "hi": "1023", "bn": "1056", "gu": "1072", "kn": "1086",
    "mr": "1102", "pa": "1110", "ta": "1130", "te": "1131",
    "hinglish": "1023",           # Roman-script Hindi ranks under Hindi
}

COMPETITION = {0: "", 1: "UNSPECIFIED", 2: "LOW", 3: "MEDIUM", 4: "HIGH"}

FIELDS = ["keyword", "lang", "script", "intent", "source_heading",
          "volume_in", "competition", "cpc_inr", "source", "checked_on"]

# Question shapes people actually type, per language. These become the FAQ and
# question-heading seeds -- the queries an answer engine is matching against.
QUESTION_STEMS = {
    "hi": ["{} के लक्षण", "{} का इलाज", "{} क्यों होता है", "{} में क्या खाएं",
           "{} कैसे ठीक करें", "{} के घरेलू उपाय"],
    "mr": ["{} ची लक्षणे", "{} वर उपचार", "{} का होतो", "{} मध्ये काय खावे"],
    "gu": ["{} ના લક્ષણો", "{} ની સારવાર", "{} કેમ થાય છે"],
    "bn": ["{} এর লক্ষণ", "{} এর চিকিৎসা", "{} কেন হয়"],
    "ta": ["{} அறிகுறிகள்", "{} சிகிச்சை", "{} ஏன் வருகிறது"],
    "te": ["{} లక్షణాలు", "{} చికిత్స", "{} ఎందుకు వస్తుంది"],
    "kn": ["{} ಲಕ್ಷಣಗಳು", "{} ಚಿಕಿತ್ಸೆ", "{} ಏಕೆ ಬರುತ್ತದೆ"],
    "pa": ["{} ਦੇ ਲੱਛਣ", "{} ਦਾ ਇਲਾਜ", "{} ਕਿਉਂ ਹੁੰਦਾ ਹੈ"],
    "hinglish": ["{} ke lakshan", "{} ka ilaj", "{} kyu hota hai",
                 "{} me kya khaye", "{} ke gharelu upay"],
}


# Interrogative and filler openings to strip so a heading yields a topic rather
# than a whole sentence. "What are the first symptoms of diabetes?" must seed
# "diabetes symptoms", not "What are the first symptoms of diabetes के लक्षण".
_QUESTION_OPENERS = re.compile(
    r"^\s*(what|why|how|when|where|which|who|can|is|are|does|do|should|will|"
    r"क्या|कैसे|क्यों|कब|कहाँ|कौन|किस|"
    r"શું|કેવી|કેમ|કિ|"
    r"কি|কীভাবে|কেন|"
    r"என்ன|எப்படி|ஏன்|"
    r"ఏమిటి|ఎలా|ఎందుకు|"
    r"ಏನು|ಹೇಗೆ|ಏಕೆ|"
    r"ਕੀ|ਕਿਵੇਂ|ਕਿਉਂ)\b[\s,]*", re.I)

# Tokens that carry no search intent and can be shaved off either end.
# Indic languages are head-initial for the topic and put the interrogative and
# auxiliary verbs at the END: "मधुमेह के पहले लक्षण क्या हैं?" -- the topic is
# मधुमेह, at the front. English is the mirror image: "What are the first
# symptoms of diabetes?" puts the topic last. Stripping the same end in both
# would keep precisely the wrong half, which is what the first version did.
_TRAILING_NOISE = {
    "क्या", "हैं", "है", "हो", "करें", "चाहिए", "नहीं", "सकता", "सकती", "सकते",
    "होता", "होती", "करना", "कर", "को", "जिन्हें", "जो", "वाले", "गया", "रहा",
    "आहे", "आहेत", "काय", "छे", "શું", "কি", "হয়", "என்ன", "ஏன்", "ఏమిటి",
    "ಏನು", "ਕੀ", "ਹੈ", "ਹਨ",
}
_LEADING_NOISE = {"the", "a", "an", "your", "you", "top", "best", "common"}

_MAX_SEED_WORDS = 5


def _clean_tok(word: str) -> str:
    return word.strip(" ,।॥?.!:;\"'()").lower()


def _topic(text: str, script: str = "Latin") -> str:
    """Reduce a heading to the phrase someone would actually search."""
    t = re.sub(r"^\s*\d+[.)]\s*", "", text or "").strip(" ?:।॥.!")
    words = [w for w in t.split() if w]
    indic = script != "Latin"

    def strip_trailing() -> None:
        while words and _clean_tok(words[-1]) in _TRAILING_NOISE:
            words.pop()

    def strip_leading() -> None:
        noise = _LEADING_NOISE | (_TRAILING_NOISE if indic else set())
        # \b does not behave for Devanagari in Python's re, so leading
        # interrogatives are matched token-wise rather than by regex.
        while words and (_clean_tok(words[0]) in noise
                         or _QUESTION_OPENERS.match(words[0] + " ")):
            words.pop(0)

    strip_leading()
    strip_trailing()

    if len(words) > _MAX_SEED_WORDS:
        # Indic puts the topic at the front, English at the end.
        words = words[:_MAX_SEED_WORDS] if indic else words[-_MAX_SEED_WORDS:]

    # Cutting to length can leave a dangling connective ("... लक्षण जिन्हें").
    strip_trailing()
    strip_leading()

    return " ".join(words).strip(" ,-–—")


def _seeds(art: Article, script: str = "Latin") -> list[tuple[str, str]]:
    """(term, source_heading) pairs worth building queries around.

    Seeds come from whatever article is passed in, so run.py passes the
    TRANSLATED article: seeding from English headings and then appending Hindi
    question stems produces "diabetes symptoms के लक्षण", which is not a query
    any person has ever typed.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(term: str, src: str) -> None:
        term = re.sub(r"\s+", " ", term).strip()
        if 2 < len(term) < 60 and term.lower() not in seen:
            seen.add(term.lower())
            out.append((term, src))

    add(_topic(art.title, script), "title")
    for h in art.headings():
        add(_topic(h, script), "heading")
    return out[:10]


def for_article(art: Article, lang: str) -> list[dict]:
    """Seed keyword set for one language. Volumes left blank until filled."""
    entry = lang_by_code(lang)
    rows: list[dict] = []
    seen: set[str] = set()

    def add(kw: str, intent: str, src: str) -> None:
        kw = re.sub(r"\s+", " ", kw).strip()
        if not kw or kw.lower() in seen or len(kw) > 90:
            return
        seen.add(kw.lower())
        rows.append({"keyword": kw, "lang": lang, "script": entry["script"],
                     "intent": intent, "source_heading": src,
                     "volume_in": "", "competition": "", "cpc_inr": "",
                     "source": "", "checked_on": ""})

    for term, src in _seeds(art, entry["script"]):
        add(term, "head", src)
        term_toks = {_clean_tok(w) for w in term.split()}
        for stem in QUESTION_STEMS.get(lang, []):
            # Skip a stem whose content word is already in the seed, or
            # "मधुमेह के पहले लक्षण" becomes "मधुमेह के पहले लक्षण के लक्षण".
            stem_toks = {_clean_tok(w) for w in stem.replace("{}", "").split()}
            content = stem_toks - _TRAILING_NOISE - {"के", "का", "की", "में", "ke", "ka", "me"}
            if content & term_toks:
                continue
            add(stem.format(term), "question", src)

    return rows


# ------------------------------------------------------------- Google Ads

def _verify_language(client, lang_id: str, expected_name: str) -> bool:
    """Confirm a languageConstant id really is the language we think it is."""
    try:
        ga = client.get_service("GoogleAdsService")
        rows = ga.search(
            customer_id=_customer_id(),
            query=("SELECT language_constant.name, language_constant.code "
                   f"FROM language_constant WHERE language_constant.id = {lang_id}"))
        for row in rows:
            got = (row.language_constant.name or "").lower()
            if expected_name.lower() in got or got in expected_name.lower():
                return True
            warn(f"languageConstant {lang_id} is '{got}', expected "
                 f"'{expected_name}' -- skipping volumes for this language "
                 "rather than reporting another language's numbers")
            return False
    except Exception as exc:
        warn(f"could not verify languageConstant {lang_id}: {exc.__class__.__name__}")
    return False


_CUSTOMER: str | None = None


def _customer_id() -> str:
    global _CUSTOMER
    if _CUSTOMER:
        return _CUSTOMER
    import os
    cid = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
    if not cid and (ROOT / "google_ads_customer_id.txt").exists():
        cid = (ROOT / "google_ads_customer_id.txt").read_text(encoding="utf-8").strip()
    _CUSTOMER = cid.replace("-", "")
    return _CUSTOMER


def require_ads() -> None:
    if not YAML_PATH.exists() or not _customer_id():
        raise MissingCredential(
            "Google Ads Keyword Planner credentials",
            "real search volume, competition and CPC for the regional keyword sets",
            "Create aeo-translator/google-ads.yaml (developer_token, client_id, "
            "client_secret, refresh_token, login_customer_id, use_proto_plus: True) "
            "and put the account id in google_ads_customer_id.txt or "
            "GOOGLE_ADS_CUSTOMER_ID. The same file already works for "
            "C:/Claude/hiims-seo -- copy it across. Until then volume columns stay "
            "empty; nothing is estimated.")


def fill_volumes(rows: list[dict], lang: str) -> list[dict]:
    """Attach real Keyword Planner metrics. Raises MissingCredential if unset."""
    require_ads()
    try:
        from google.ads.googleads.client import GoogleAdsClient
        from google.ads.googleads.errors import GoogleAdsException
    except ImportError:
        raise MissingCredential(
            "the google-ads Python package",
            "querying Keyword Planner",
            "pip install google-ads")

    entry = lang_by_code(lang)
    lang_id = LANG_CONSTANTS.get(lang)
    if not lang_id:
        warn(f"no Google Ads languageConstant mapped for {lang}; volumes skipped")
        return rows

    client = GoogleAdsClient.load_from_storage(str(YAML_PATH))
    expected = "Hindi" if lang == "hinglish" else entry["name"]
    if not _verify_language(client, lang_id, expected):
        return rows

    svc = client.get_service("KeywordPlanIdeaService")
    keywords = [r["keyword"] for r in rows]
    metrics: dict[str, dict] = {}

    for i in range(0, len(keywords), 20):        # API caps seeds at 20
        batch = keywords[i:i + 20]
        req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
        req.customer_id = _customer_id()
        req.keywords.extend(batch)
        req.geo_target_constants.append(f"geoTargetConstants/{GEO_INDIA}")
        req.language = f"languageConstants/{lang_id}"
        req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        try:
            resp = svc.generate_keyword_historical_metrics(request=req)
        except GoogleAdsException as exc:
            warn(f"Keyword Planner error ({exc.error.code().name}); "
                 "volumes left empty for this batch")
            for err in exc.failure.errors[:3]:
                warn(f"  {err.message}")
            continue
        for res in resp.results:
            m = res.keyword_metrics
            if not m:
                continue
            metrics[res.text.lower()] = {
                "volume_in": m.avg_monthly_searches or 0,
                "competition": COMPETITION.get(int(m.competition), ""),
                "cpc_inr": round((m.high_top_of_page_bid_micros or 0) / 1e6, 2),
            }

    today = datetime.date.today().isoformat()
    hit = 0
    for r in rows:
        m = metrics.get(r["keyword"].lower())
        if not m:
            continue
        r.update(m)
        r["source"] = "Google Keyword Planner"
        r["checked_on"] = today
        hit += 1

    log(f"keywords: filled {hit}/{len(rows)} volumes for {lang}", indent=1)
    return rows


def save_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in FIELDS} for r in rows])
    return path


def build(art: Article, lang: str, *, with_volumes: bool = True) -> list[dict]:
    """Seed set plus volumes when the credentials exist, blanks when they do not."""
    rows = for_article(art, lang)
    if with_volumes:
        try:
            rows = fill_volumes(rows, lang)
        except MissingCredential as exc:
            warn(f"keyword volumes unavailable: {exc.what}. "
                 "Columns left empty -- nothing estimated.")
    return rows
