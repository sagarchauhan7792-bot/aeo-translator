"""HTML -> a clean, ordered Article model.

Block order is the contract. Everything downstream (translation chunking, the
rewrite loop, Google Docs rendering) reassembles by index, so a block that is
dropped or reordered here corrupts every later stage silently.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from bs4 import BeautifulSoup

from common import slugify, word_count, warn

BLOCK_TAGS = {
    "h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3",
    "p": "p", "li": "li", "blockquote": "quote", "pre": "code",
}

# Question shapes across the languages we target, used to detect existing FAQs
# and to score how AEO-ready a document already is.
QUESTION_RX = re.compile(
    r"(\?|？|"
    r"^(what|why|how|when|where|which|who|can|is|are|does|do|should|will)\b|"
    r"\b(क्या|कैसे|क्यों|कब|कहाँ|कौन|किस)\b|"           # Hindi / Marathi
    r"\b(શું|કેવી|કેમ|ક્યારે)\b|"                        # Gujarati
    r"\b(কি|কীভাবে|কেন|কখন)\b|"                        # Bengali
    r"\b(என்ன|எப்படி|ஏன்|எப்போது)\b|"                   # Tamil
    r"\b(ఏమిటి|ఎలా|ఎందుకు|ఎప్పుడు)\b|"                 # Telugu
    r"\b(ಏನು|ಹೇಗೆ|ಏಕೆ|ಯಾವಾಗ)\b|"                       # Kannada
    r"\b(ਕੀ|ਕਿਵੇਂ|ਕਿਉਂ|ਕਦੋਂ)\b)",                       # Punjabi
    re.I | re.M,
)


def is_question(text: str) -> bool:
    return bool(QUESTION_RX.search((text or "").strip()))


@dataclass
class Block:
    type: str          # h1|h2|h3|p|li|quote|code|answer|tldr
    text: str

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class Article:
    slug: str = ""
    lang: str = "en"
    title: str = ""
    meta_description: str = ""
    author: str = ""
    author_credentials: str = ""
    published: str = ""
    source_url: str = ""
    source_type: str = "url"
    blocks: list[Block] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    faqs: list[dict] = field(default_factory=list)
    keywords: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    # ---- derived views ----------------------------------------------------
    def body_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())

    def full_text(self) -> str:
        return f"{self.title}\n\n{self.body_text()}"

    def words(self) -> int:
        return word_count(self.full_text())

    def headings(self) -> list[str]:
        return [b.text for b in self.blocks if b.type in ("h2", "h3")]

    # ---- serialisation ----------------------------------------------------
    def dict(self) -> dict:
        d = asdict(self)
        d["blocks"] = [b.dict() if isinstance(b, Block) else b for b in self.blocks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        d = dict(d)
        d["blocks"] = [Block(**b) if isinstance(b, dict) else b for b in d.get("blocks", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Article":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _blocks_from_html(html: str) -> tuple[list[Block], list[dict], list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    blocks: list[Block] = []
    images: list[dict] = []
    links: list[dict] = []

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src:
            images.append({"src": src, "alt": _clean(img.get("alt", ""))})

    for a in soup.find_all("a", href=True):
        text = _clean(a.get_text())
        if text:
            links.append({"href": a["href"], "text": text})

    for el in soup.find_all(list(BLOCK_TAGS)):
        # A <li> inside a <blockquote> would otherwise be emitted twice.
        if el.find_parent(["blockquote", "pre"]) and el.name not in ("blockquote", "pre"):
            continue
        text = _clean(el.get_text(" "))
        if not text or len(text) < 2:
            continue
        blocks.append(Block(type=BLOCK_TAGS[el.name], text=text))

    return blocks, images, links


def _dedupe_blocks(blocks: list[Block]) -> list[Block]:
    """Drop consecutive identical blocks -- a common artefact of nested markup."""
    out: list[Block] = []
    for b in blocks:
        if out and out[-1].type == b.type and out[-1].text == b.text:
            continue
        out.append(b)
    return out


def detect_faqs(blocks: list[Block]) -> list[dict]:
    """Pair question-shaped headings with the prose that answers them."""
    faqs: list[dict] = []
    for i, b in enumerate(blocks):
        if b.type not in ("h2", "h3") or not is_question(b.text):
            continue
        answer_parts = []
        for nxt in blocks[i + 1:]:
            if nxt.type in ("h1", "h2", "h3"):
                break
            if nxt.type in ("p", "li"):
                answer_parts.append(nxt.text)
            if len(" ".join(answer_parts)) > 700:
                break
        if answer_parts:
            faqs.append({"q": b.text, "a": " ".join(answer_parts).strip()})
    return faqs


def from_html(html: str, url: str = "", *, source_type: str = "url") -> Article:
    """Extract an Article from a full page of HTML.

    trafilatura strips nav/footer/sidebar and returns just the article body; we
    then re-parse that reduced HTML for structure. Falling straight to
    BeautifulSoup on the raw page would drag menus and cookie banners into the
    translation, which is expensive and reads as garbage in every language.
    """
    import trafilatura

    art = Article(source_url=url, source_type=source_type)

    try:
        md = trafilatura.extract_metadata(html)
        if md:
            art.title = _clean(md.title or "")
            art.author = _clean(md.author or "")
            art.published = (md.date or "")
            art.meta_description = _clean(md.description or "")
            art.meta["sitename"] = md.sitename or ""
    except Exception as exc:                       # metadata is nice-to-have
        warn(f"metadata extraction failed ({exc.__class__.__name__}); continuing")

    body_html = trafilatura.extract(
        html,
        output_format="html",
        include_links=True,
        include_images=True,
        include_formatting=True,
        include_tables=True,
        favor_recall=True,
        url=url or None,
    )

    if body_html:
        blocks, images, links = _blocks_from_html(body_html)
    else:
        warn("trafilatura found no article body; falling back to raw <article>/<main>")
        soup = BeautifulSoup(html, "lxml")
        for junk in soup.find_all(["nav", "footer", "header", "aside", "script", "style", "form"]):
            junk.decompose()
        container = soup.find("article") or soup.find("main") or soup.body or soup
        blocks, images, links = _blocks_from_html(str(container))

    blocks = _dedupe_blocks(blocks)

    # The page <h1> is the title, not a body block -- keeping it would translate
    # and render the headline twice.
    if not art.title:
        for b in blocks:
            if b.type == "h1":
                art.title = b.text
                break
    if not art.title:
        soup = BeautifulSoup(html, "lxml")
        art.title = _clean(soup.title.get_text() if soup.title else "")
    blocks = [b for b in blocks if b.type != "h1"]

    if not art.meta_description:
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"})
        if tag and tag.get("content"):
            art.meta_description = _clean(tag["content"])

    art.blocks = blocks
    art.images = images
    art.links = links
    art.faqs = detect_faqs(blocks)
    art.slug = slugify(art.title) or slugify(url.rsplit("/", 1)[-1] if url else "article")
    return art


def from_markdown(text: str, url: str = "", *, source_type: str = "file") -> Article:
    """Parse a pasted/manual post: markdown or plain text."""
    art = Article(source_url=url, source_type=source_type)
    blocks: list[Block] = []

    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            head = line.lstrip("#").strip()
            if level == 1 and not art.title:
                art.title = head
                continue
            blocks.append(Block(type="h2" if level <= 2 else "h3", text=head))
        elif re.match(r"^\s*[-*+]\s+", line):
            blocks.append(Block(type="li", text=re.sub(r"^\s*[-*+]\s+", "", line).strip()))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            blocks.append(Block(type="li", text=re.sub(r"^\s*\d+[.)]\s+", "", line).strip()))
        elif line.startswith(">"):
            blocks.append(Block(type="quote", text=line.lstrip("> ").strip()))
        else:
            blocks.append(Block(type="p", text=line.strip()))

    if not art.title and blocks:
        art.title = blocks.pop(0).text

    art.blocks = _dedupe_blocks(blocks)
    art.faqs = detect_faqs(art.blocks)
    art.slug = slugify(art.title)
    return art
