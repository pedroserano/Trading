"""The guides in docs/, shown inside the app so they open without GitHub and match the
installed version."""

import re
from pathlib import Path

from markdown_it import MarkdownIt

# The docs folder: /app/docs in the image, the repository's docs/ when run from a checkout.
_HERE = Path(__file__).resolve().parent
DOCS_DIRS = (_HERE.parent / "docs", _HERE.parent.parent / "docs")
GUIDES = {
    "ai": ("AI.md", "AI access"),
    "deploy": ("DEPLOY-TRUENAS.md", "Install on TrueNAS"),
}
_BY_FILE = {f: name for name, (f, _) in GUIDES.items()}
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def _slug(text: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to dashes."""
    text = re.sub(r"<[^>]+>", "", text).lower().strip()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _heading_ids(html: str) -> str:
    def add(m: re.Match) -> str:
        return f'<h{m[1]} id="{_slug(m[2])}">{m[2]}</h{m[1]}>'

    return re.sub(r"<h([1-4])>(.*?)</h\1>", add, html)


def _links(html: str) -> str:
    """Links between guides stay inside the app; outside links open a new tab."""

    def fix(m: re.Match) -> str:
        href = m[1]
        file, _, anchor = href.partition("#")
        if file in _BY_FILE:
            return f'href="/guide/{_BY_FILE[file]}{"#" + anchor if anchor else ""}"'
        if href.startswith(("http://", "https://")):
            return f'href="{href}" target="_blank" rel="noopener"'
        return m[0]

    return re.sub(r'href="([^"]*)"', fix, html)


def render(name: str) -> tuple[str, str] | None:
    """(title, HTML) for a guide, or None when there is no such guide or file."""
    if name not in GUIDES:
        return None
    file, title = GUIDES[name]
    for d in DOCS_DIRS:
        path = d / file
        if path.is_file():
            return title, _links(_heading_ids(_md.render(path.read_text(encoding="utf-8"))))
    return None
