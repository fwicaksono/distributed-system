"""Build docs/tutorial.html — a single, self-contained, offline tutorial.

    python3 docs/tutorial/build.py

Sources: docs/tutorial/template.html + docs/tutorial/src/*.html (sorted by name).
Only the standard library is used.

Custom tags expanded at build time (so source files stay readable):

  <x-code lang="bash" title="optional caption">raw text, no escaping needed</x-code>
  <x-out>expected output, raw text</x-out>
  <x-file path="nginx/nginx.conf" lang="nginx" lines="10-40"></x-file>
        -> embeds the REAL file from the repository, so the tutorial can't drift from the code
  <x-note kind="info|tip|warn|danger" title="...">HTML</x-note>

Sections:  <section class="step" id="slug" data-phase="Phase name" data-title="Title"> ...
The table of contents and step numbers are generated from these attributes.
"""

from __future__ import annotations

import html
import re
import textwrap
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
OUT = ROOT / "docs" / "tutorial.html"

CODE_RE = re.compile(r'<x-code(?P<attrs>[^>]*)>(?P<body>.*?)</x-code>', re.S)
OUT_RE = re.compile(r'<x-out(?P<attrs>[^>]*)>(?P<body>.*?)</x-out>', re.S)
FILE_RE = re.compile(r'<x-file(?P<attrs>[^>]*)>\s*</x-file>', re.S)
NOTE_RE = re.compile(r'<x-note(?P<attrs>[^>]*)>(?P<body>.*?)</x-note>', re.S)
ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')
SECTION_RE = re.compile(r'<section class="step" id="([^"]+)" data-phase="([^"]+)" data-title="([^"]+)"')

NOTE_LABELS = {"info": "Catatan", "tip": "Tip", "warn": "Perhatian", "danger": "Bahaya"}


def attrs(raw: str) -> dict[str, str]:
    return dict(ATTR_RE.findall(raw))


def clean(body: str) -> str:
    # Raw text; entities typed out of habit (&lt; &amp;) are decoded so they display correctly.
    return html.unescape(textwrap.dedent(body.strip("\n")).rstrip())


def code_block(text: str, lang: str, title: str = "", kind: str = "code") -> str:
    caption = (
        f'<figcaption><span class="lang">{html.escape(lang)}</span>{html.escape(title)}</figcaption>'
        if (title or lang) else ""
    )
    return (
        f'<figure class="{kind}">{caption}'
        f'<button class="copy" type="button" aria-label="Salin ke clipboard">Salin</button>'
        f"<pre><code>{html.escape(text)}</code></pre></figure>"
    )


def expand(source: str) -> str:
    def code(m: re.Match[str]) -> str:
        a = attrs(m["attrs"])
        return code_block(clean(m["body"]), a.get("lang", ""), a.get("title", ""))

    def output(m: re.Match[str]) -> str:
        a = attrs(m["attrs"])
        return code_block(clean(m["body"]), "output", a.get("title", "Output yang diharapkan"), "output")

    def file(m: re.Match[str]) -> str:
        a = attrs(m["attrs"])
        path = ROOT / a["path"]
        text = path.read_text()
        if "lines" in a:
            start, end = (int(x) for x in a["lines"].split("-"))
            text = "\n".join(text.splitlines()[start - 1:end])
        return code_block(text.rstrip(), a.get("lang", path.suffix.lstrip(".")), a["path"], "code file")

    def note(m: re.Match[str]) -> str:
        a = attrs(m["attrs"])
        kind = a.get("kind", "info")
        title = a.get("title") or NOTE_LABELS[kind]
        return (f'<aside class="note {kind}" role="note"><p class="note-title">{html.escape(title)}</p>'
                f'{m["body"].strip()}</aside>')

    source = CODE_RE.sub(code, source)
    source = OUT_RE.sub(output, source)
    source = FILE_RE.sub(file, source)
    return NOTE_RE.sub(note, source)


def main() -> None:
    parts = [expand(p.read_text()) for p in sorted((HERE / "src").glob("*.html"))]
    content = "\n".join(parts)

    sections = SECTION_RE.findall(content)
    toc: list[str] = []
    current_phase = None
    for number, (slug, raw_phase, raw_title) in enumerate(sections, start=1):
        opening = f'<section class="step" id="{slug}" data-phase="{raw_phase}" data-title="{raw_title}">'
        # Attribute values are already HTML-escaped in the sources; decode once before re-escaping.
        phase, title = html.unescape(raw_phase), html.unescape(raw_title)
        head = (f'{opening}\n<div class="step-head"><h2 data-num="{number}">{html.escape(title)}</h2>'
                f'<span class="phase-tag">{html.escape(phase)}</span></div>'
                f'<label class="mark" for="done-{slug}"><input type="checkbox" id="done-{slug}"> '
                f"Tandai langkah {number} selesai</label>")
        content = content.replace(opening, head, 1)
        if phase != current_phase:
            if current_phase is not None:
                toc.append("</ol></li>")
            toc.append(f'<li class="phase"><span class="phase-name">{html.escape(phase)}</span><ol>')
            current_phase = phase
        toc.append(f'<li><a href="#{slug}" data-slug="{slug}"><span class="n">{number}</span>'
                   f"{html.escape(title)}</a></li>")
    toc.append("</ol></li>")

    template = (HERE / "template.html").read_text()
    page = (template.replace("{{TOC}}", "\n".join(toc))
                    .replace("{{CONTENT}}", content)
                    .replace("{{STEP_COUNT}}", str(len(sections)))
                    .replace("{{BUILD_DATE}}", date.today().isoformat()))
    OUT.write_text(page)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(sections)} sections, {len(page) // 1024} KiB")


if __name__ == "__main__":
    main()
