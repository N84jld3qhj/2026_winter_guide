#!/usr/bin/env python3
"""Static multi-page builder for the 2026 로아온 가이드.

Single source of truth:
  templates/template.html   common page skeleton (head + sidebar + main + footer)
  templates/_scripts.html   shared footer (progress bar, scripts, fixed UI)
  style.css                 shared styles
  content/intro.html        landing intro block
  content/sectionN.html     one body fragment per top-level section

Output (flat, at dist/):
  dist/index.html           landing = intro + section TOC grid
  dist/sectionN.html        one page per section, with a 2-depth in-page index
  dist/style.css            copied
  dist/images/              copied from ./images
  dist/.nojekyll            disables Jekyll on GitHub Pages

The build is deterministic and idempotent: running it twice yields identical
output. Image URLs that pointed at raw.githubusercontent.com are rewritten to
the local relative `images/...` path so the site is self-contained and works
under the GitHub Pages project sub-path.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import markdown

ROOT = pathlib.Path(__file__).resolve().parent
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"
DIST = ROOT / "dist"

# Top-level navigation / page table. Order = display order.
# (page_id, output filename, short nav label, content fragment, <title>)
SITE_TITLE = "2026 썸머 로아온 뉴비/복귀 가이드"
SECTIONS = [
    ("section0", "section0.md", "0. 모코코 베이스 캠프 가이드", "section0.md", "0. 모코코 베이스 캠프 가이드"),
    ("section1", "section1.md", "1. 과금 요소", "section1.md", "1. 과금 요소"),
    ("section2", "section2.md", "2. 공식 게임 가이드", "section2.md", "2. 공식 게임 가이드"),
    ("section3", "section3.md", "3. 인게임 설정", "section3.md", "3. 인게임 설정"),
    ("section4", "section4.md", "4. 일일 콘텐츠", "section4.md", "4. 일일 콘텐츠"),
    ("section5", "section5.md", "5. 주간 콘텐츠", "section5.md", "5. 주간 콘텐츠"),
    ("section6", "section6.md", "6. 캘린더 콘텐츠", "section6.md", "6. 캘린더 콘텐츠"),
    ("section7", "section7.md", "7. 골드 수급처", "section7.md", "7. 골드 수급처"),
    ("section8", "section8.md", "8. 스펙업", "section8.md", "8. 스펙업"),
    ("section9", "section9.md", "9. 내실", "section9.md", "9. 내실"),
    ("section10", "section10.md", "10. 외부 사이트", "section10.md", "10. 외부 사이트"),
]
LANDING_ID = "index"
LANDING_FILE = "index.md"

RAW_IMG_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/[^/]+/[^/]+/[^/]+/images/",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
ATTR_ID_RE = re.compile(r'\bid="([^"]+)"')
HEADING_RE = re.compile(
    r'<(h[23])\b([^>]*)\bclass="(sub-title|section-header)"([^>]*)>(.*?)</\1>',
    re.IGNORECASE | re.DOTALL,
)
EXISTING_ID_RE = re.compile(r'\bid="([^"]+)"')
HREF_ANCHOR_RE = re.compile(r'href="#([^"]+)"')
# inline hardcoded link color (single-accent design: let CSS theme links via token)
INLINE_LINK_STYLE_RE = re.compile(r'\s*style="color:\s*#3b82f6;[^"]*"')
NUM_PREFIX_RE = re.compile(r'^\s*\d+\.\s*')
MAIN_TITLE_RE = re.compile(
    r'<h1\b[^>]*\bclass="main-title"[^>]*\bid="([^"]+)"[^>]*>(.*?)</h1>',
    re.IGNORECASE | re.DOTALL,
)


def section_num(pid: str) -> int | None:
    m = re.match(r"section(\d+)$", pid)
    return int(m.group(1)) if m else None


def short_label(label: str) -> str:
    """Drop a leading 'N. ' so the number lives only in the badge/chip."""
    return NUM_PREFIX_RE.sub("", label).strip()


# Signature: left spine badges colored along the Lost Ark rarity ladder
# (decorative rarity spectrum across the 10 sections; active row stays gold).
SPINE_TIERS = [
    "common", "uncommon", "rare", "heroic", "legendary",
    "relic", "ancient", "esther", "legendary", "ancient",
]

# Conservative in-content grade coloring (in-game item-name color behavior).
# Only color these grade words, and only when immediately followed by a
# real grade/item noun — so generic uses ("일반적으로", "일반 퀘스트", "고대로")
# are never touched. 일반/고급 are excluded entirely (too ambiguous).
GRADE_TIERS = {
    "전설": "legendary", "유물": "relic", "고대": "ancient",
    "영웅": "heroic", "희귀": "rare", "에스더": "esther",
}
_GRADE_NOUNS = (
    "등급|코어|카드|각인서|악세사리|악세|장신구|장비|무기|방어구|아바타|"
    "엘릭서|세트|젬|보석|도구|재료|품질|돌파석|전투|악세서리|코어들|초월"
)
GRADE_RE = re.compile(
    r"(전설|유물|고대|영웅|희귀|에스더)(?=\s+(?:" + _GRADE_NOUNS + r"))"
)


def colorize_grades(html: str) -> str:
    """Wrap a grade word in its tier color span (content preserved — text only)."""
    def _c(m: re.Match) -> str:
        w = m.group(1)
        return f'<span class="tier tier-{GRADE_TIERS[w]}">{w}</span>'
    return GRADE_RE.sub(_c, html)


# Full-text search index: split a page into heading-bounded chunks.
CHUNK_HEADING_RE = re.compile(
    r'<(h[123])\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</\1>',
    re.IGNORECASE | re.DOTALL,
)
WS_RE = re.compile(r"\s+")
PLACEHOLDER_RE = re.compile(
    r'<div class="image-placeholder">.*?</div>', re.IGNORECASE | re.DOTALL
)


def _plain(segment: str) -> str:
    """Strip tags (incl. <img>, so alt text never leaks) + normalize whitespace.
    Drops 'image-placeholder' boilerplate ('이미지 준비 중') so it never pollutes search."""
    import html as _html
    segment = PLACEHOLDER_RE.sub(" ", segment)
    return WS_RE.sub(" ", _html.unescape(TAG_RE.sub(" ", segment))).strip()


def build_chunks(page_html: str, fname: str, page_title: str) -> list[dict]:
    """One search entry per heading: {file, anchor, page, heading, text}.
    text = body between this heading and the next (tags stripped)."""
    heads = list(CHUNK_HEADING_RE.finditer(page_html))
    chunks: list[dict] = []
    # preamble before the first heading (rare) -> page top
    if heads and heads[0].start() > 0:
        pre = _plain(page_html[: heads[0].start()])
        if pre:
            chunks.append({"file": fname, "anchor": "", "page": page_title,
                           "heading": page_title, "text": pre})
    for i, m in enumerate(heads):
        anchor = m.group(2)
        heading = _plain(m.group(3))
        end = heads[i + 1].start() if i + 1 < len(heads) else len(page_html)
        text = _plain(page_html[m.end():end])
        chunks.append({"file": fname, "anchor": anchor, "page": page_title,
                       "heading": heading or page_title, "text": text})
    return chunks


# Author inline styles in the content hardcode hex + 4px radius. Re-point the
# values at theme tokens (so they adapt light/dark) and cap radius to sharp,
# WITHOUT touching markup structure (no class conflicts, text preserved).
INLINE_RADIUS_RE = re.compile(r"border-radius:\s*\d+px")


def theme_inline_styles(html: str) -> str:
    html = html.replace("#fef3c7", "var(--accent-weak)")  # yellow highlight bg
    html = html.replace("color: #111", "color: var(--text)")
    html = html.replace("color:#e4bd61", "color: var(--tier-legendary)")  # 보석 진화
    html = html.replace("color:#28c6ff", "color: var(--tier-rare)")       # 깨달음
    html = html.replace("color:#20e500", "color: var(--tier-uncommon)")   # 도약
    html = INLINE_RADIUS_RE.sub("border-radius: 2px", html)
    return html


def slugify(text: str, seen: set[str]) -> str:
    """Stable, Korean-friendly anchor slug. Dedupes within `seen`."""
    text = TAG_RE.sub("", text)
    text = text.strip().lower()
    # keep ascii word chars and Hangul syllables, everything else -> '-'
    text = re.sub(r"[^\w가-힣]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        text = "sec"
    base = text
    i = 2
    while text in seen:
        text = f"{base}-{i}"
        i += 1
    seen.add(text)
    return text


def rewrite_images(html: str) -> str:
    return RAW_IMG_RE.sub("images/", html)


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> int:
    # --- pass 1: load fragments, assign slugs, build global id registry ---
    # registry maps an original anchor target (text after '#') -> final href
    registry: dict[str, str] = {LANDING_ID: LANDING_FILE}
    for pid, fname, _label, _frag, _title in SECTIONS:
        registry[pid] = fname

    pages: list[dict] = []
    # landing intro fragment is processed like a page but rendered specially
    fragments = [("intro", "intro.html", LANDING_FILE)] + [
        (pid, frag, fname) for pid, fname, _l, frag, _t in SECTIONS
    ]

    raw_by_id: dict[str, str] = {}  # page_id -> html with ids injected
    index_by_id: dict[str, list[tuple[str, str, str]]] = {}

    # 마크다운 파서 초기화 (테이블, 파스 코드블록 등 확장 지원)
    md_converter = markdown.Markdown(extensions=['tables', 'fenced_code', 'toc'])

    for pid, frag, fname in fragments:
        raw_md = read(CONTENT / frag)
        # 마크다운을 HTML로 변환
        html = md_converter.convert(raw_md)
        md_converter.reset() # 상태 초기화
        
        seen: set[str] = set()
        # reserve the page-level id and any pre-existing element ids first,
        # remapping them to clean slugs and recording them in the registry
        for existing in EXISTING_ID_RE.findall(html):
            if existing in registry:  # page-level ids already handled
                continue
            slug = slugify(existing, seen)
            html = html.replace(f'id="{existing}"', f'id="{slug}"', 1)
            registry[existing] = f"{fname}#{slug}"

        # inject ids onto h2/h3 headings and collect the 2-depth index
        entries: list[tuple[str, str, str]] = []  # (level, slug, text)

        def _inject(m: re.Match) -> str:
            tag, pre, cls, post, inner = m.groups()
            label = TAG_RE.sub("", inner).strip()
            # heading already carries an id (slugified in pass 1) -> reuse it,
            # never inject a second id attribute (would be invalid HTML and the
            # 2-depth index would link the ignored duplicate).
            existing = ATTR_ID_RE.search(pre) or ATTR_ID_RE.search(post)
            if existing:
                entries.append((tag.lower(), existing.group(1), label))
                return m.group(0)
            slug = slugify(label, seen)
            attrs = f'{pre} class="{cls}"{post}'.rstrip()
            entries.append((tag.lower(), slug, label))
            return f'<{tag}{attrs} id="{slug}">{inner}</{tag}>'

        html = HEADING_RE.sub(_inject, html)
        raw_by_id[pid] = html
        index_by_id[pid] = entries

    # --- pass 2: rewrite internal anchors + images, render pages ---
    def finalize(html: str) -> str:
        def _href(m: re.Match) -> str:
            target = m.group(1)
            if target in registry:
                return f'href="{registry[target]}"'
            return m.group(0)  # same-page anchor; leave as-is
        html = HREF_ANCHOR_RE.sub(_href, html)
        html = INLINE_LINK_STYLE_RE.sub("", html)
        html = theme_inline_styles(html)
        html = colorize_grades(html)
        return rewrite_images(html)

    template = read(TEMPLATES / "template.html")
    scripts = read(TEMPLATES / "_scripts.html")

    DIST.mkdir(exist_ok=True)

    def spine(active: str) -> str:
        """Zone A — global 0–9 numbered spine (the signature)."""
        rows = []
        for i, (pid, fname, label, *_) in enumerate(SECTIONS):
            n = section_num(pid)
            badge = f"{n:02d}"
            tier = SPINE_TIERS[i % len(SPINE_TIERS)]
            cls = "spine-row active" if active == pid else "spine-row"
            rows.append(
                f'<a class="{cls}" href="{fname}" style="--tier: var(--tier-{tier})">'
                f'<span class="spine-badge">{badge}</span>'
                f'<span class="spine-label">{short_label(label)}</span></a>'
            )
        return "\n                ".join(rows)

    def pageindex(pid: str) -> str:
        """Zone B — right rail, h2-only on-this-page index (<details open>)."""
        h2s = [(slug, label) for level, slug, label in index_by_id.get(pid, []) if level == "h2"]
        if not h2s:
            return ""
        items = ['<details class="page-toc" id="pageToc" open>',
                 '<summary class="page-toc-summary">이 페이지 목차</summary>',
                 '<ul>']
        for slug, label in h2s:
            items.append(f'<li><a class="page-toc-link" href="#{slug}">{label}</a></li>')
        items.append('</ul>')
        items.append('</details>')
        return "\n                ".join(items)

    def breadcrumb(pid: str) -> str:
        n = section_num(pid)
        if n is None:  # landing
            return '<span class="crumb-section">목차</span>'
        label = next(short_label(l) for p, _f, l, *_ in SECTIONS if p == pid)
        return (
            f'<span class="crumb-section">섹션 {n} {label}</span>'
            f'<span class="crumb-sep" style="visibility:hidden">›</span>'
            f'<span class="crumb-current"></span>'
        )

    def with_chapter_chip(html: str, pid: str) -> str:
        """Wrap the section h1 with a 64px chapter chip; number lives in the chip."""
        n = section_num(pid)
        if n is None:
            return html

        def _wrap(m: re.Match) -> str:
            hid, inner = m.group(1), m.group(2)
            text = short_label(TAG_RE.sub("", inner).strip())
            return (
                '<div class="chapter-head">'
                f'<span class="chapter-chip" aria-hidden="true">{n:02d}</span>'
                f'<h1 class="main-title" id="{hid}">{text}</h1>'
                '</div>'
            )
        return MAIN_TITLE_RE.sub(_wrap, html, count=1)

    def render(active: str, title: str, content: str, kind: str) -> str:
        page = template
        page = page.replace("{{TITLE}}", title)
        page = page.replace("{{PAGEKIND}}", kind)
        page = page.replace("{{BREADCRUMB}}", breadcrumb(active))
        page = page.replace("{{SPINE}}", spine(active))
        page = page.replace("{{PAGEINDEX}}", pageindex(active) if kind == "section" else "")
        page = page.replace("{{CONTENT}}", content)
        page = page.replace("{{SCRIPTS}}", scripts)
        return page

    # landing page: intro fragment + numbered section list
    cards = ['<div class="section-container">',
             '<h1 class="main-title">전체 목차</h1>',
             '<ul class="landing-toc">']
    for pid, fname, label, *_ in SECTIONS:
        n = section_num(pid)
        cards.append(
            f'<li><a href="{fname}"><span class="landing-badge">{n:02d}</span>'
            f'<span>{short_label(label)}</span></a></li>'
        )
    cards.append("</ul></div>")
    landing_content = finalize(raw_by_id["intro"]) + "\n            " + "\n            ".join(cards)
    (DIST / LANDING_FILE).write_text(
        render(LANDING_ID, SITE_TITLE, landing_content, "landing"), encoding="utf-8"
    )

    # section pages
    for pid, fname, _label, _frag, title in SECTIONS:
        content = with_chapter_chip(finalize(raw_by_id[pid]), pid)
        (DIST / fname).write_text(
            render(pid, f"{title} | {SITE_TITLE}", content, "section"), encoding="utf-8"
        )

    # full-text search index: chunk each page by heading (h1/h2/h3 with id),
    # each chunk = the body text from that heading until the next heading.
    search_index = []
    for src_pid, page_title in [("intro", SITE_TITLE)] + [
            (pid, title) for pid, _f, _l, _frag, title in SECTIONS]:
        fname = LANDING_FILE if src_pid == "intro" else f"{src_pid}.html"
        search_index.extend(build_chunks(raw_by_id[src_pid], fname, page_title))
    (DIST / "search-index.json").write_text(
        json.dumps(search_index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    # static assets
    shutil.copyfile(ROOT / "style.css", DIST / "style.css")
    dist_images = DIST / "images"
    if dist_images.exists():
        shutil.rmtree(dist_images)
    shutil.copytree(ROOT / "images", dist_images)
    (DIST / ".nojekyll").write_text("", encoding="utf-8")

    pages_n = 1 + len(SECTIONS)
    print(f"built {pages_n} pages into {DIST}/ (+ style.css, images/, .nojekyll)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
