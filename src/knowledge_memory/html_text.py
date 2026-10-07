"""HTML to Markdown for imported snapshots. Structure only; no text is rewritten."""

from __future__ import annotations

import re
from html.parser import HTMLParser

TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(\s[^<>]*)?/?>")
# Markdown routinely carries an inline <a> or <br>, so a tag alone does not make a document
# HTML. Only a closed block element does; anything else is passed through untouched.
BLOCK_CLOSE = re.compile(
    r"</(?:p|div|section|article|h[1-6]|blockquote|pre|ul|ol|li|table|tr|td|th"
    r"|figure|figcaption|main|aside|header|footer)\s*>", re.I)
BLOCK = {"p", "div", "section", "article", "header", "footer", "figure", "figcaption",
         "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "li",
         "table", "tr", "hr", "br"}
DROP = {"script", "style", "head", "title", "meta", "link", "noscript"}
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "area", "base", "col",
        "embed", "param", "track", "wbr"}


def looks_like_html(text: str) -> bool:
    return BLOCK_CLOSE.search(text) is not None


class _Markdown(HTMLParser):
    """Keep headings, quotes, code, links and line structure; leave characters alone."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.quote = 0
        self.lists = []
        self.pre = 0
        self.dropped = []
        self.math = None
        self.math_block = False
        self.href = []
        self.pending = 0

    # A block boundary is remembered and only spent once real text follows it, so that
    # empty wrappers do not leave stray blank lines behind.
    def _break(self, blank=False):
        self.pending = max(self.pending, 2 if blank else 1)

    def _prefix(self):
        return "> " * self.quote + "  " * max(0, len(self.lists) - 1)

    def _write(self, text):
        if self.pending and self.out:
            blank = "> " * self.quote
            filler = ("\n" + blank.rstrip()) * (self.pending - 1)
            self.out.append(filler + "\n" + self._prefix())
        elif self.pending:
            self.out.append(self._prefix())
        self.pending = 0
        self.out.append(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        # The TeX annotation is the one thing worth keeping inside a <math> subtree.
        if tag == "annotation" and values.get("encoding") == "application/x-tex" \
                and (not self.dropped or "math" in self.dropped):
            self.math = []
            return
        if self.dropped:
            if tag not in VOID:
                self.dropped.append(tag)
            return
        if tag in DROP or "katex-html" in classes:
            self.dropped.append(tag)
            return
        if "equation" in classes or tag == "math" and values.get("display") == "block":
            self.math_block = True
        if tag == "math":
            self.dropped.append(tag)
            return
        if tag == "br":
            self._break()
        elif tag in ("p", "div", "blockquote", "pre", "ul", "ol", "table", "figure"):
            self._break(blank=True)
            if tag == "blockquote":
                self.quote += 1
            elif tag == "pre":
                self.pre += 1
                self._write("```")
                self._break()
            elif tag in ("ul", "ol"):
                self.lists.append([tag, 0])
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._break(blank=True)
            self._write("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._break()
            if self.lists:
                self.lists[-1][1] += 1
                kind, number = self.lists[-1]
                self._write(f"{number}. " if kind == "ol" else "- ")
            else:
                self._write("- ")
        elif tag == "hr":
            self._break(blank=True)
            self._write("---")
            self._break(blank=True)
        elif tag == "code" and not self.pre:
            self._write("`")
        elif tag == "a":
            self.href.append(values.get("href") or "")
            self._write("[")
        elif tag in ("td", "th") and self.out and not self.pending:
            self._write(" | ")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._break()
        elif tag == "hr":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "annotation" and self.math is not None:
            tex = "".join(self.math).strip()
            self.math = None
            if tex:
                fence = "$$" if self.math_block else "$"
                if self.math_block:
                    self._break(blank=True)
                self._write(f"{fence}{tex}{fence}")
                if self.math_block:
                    self._break(blank=True)
            self.math_block = False
            return
        if self.dropped:
            if self.dropped[-1] == tag:
                self.dropped.pop()
            return
        if tag == "blockquote":
            self.quote = max(0, self.quote - 1)
            self._break(blank=True)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._break()
            self._write("```")
            self._break(blank=True)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._break(blank=True)
        elif tag == "code" and not self.pre:
            self._write("`")
        elif tag == "a":
            target = self.href.pop() if self.href else ""
            self._write(f"]({target})" if target else "]")
        elif tag in BLOCK:
            self._break(blank=tag not in ("li", "tr"))

    def handle_data(self, data):
        if self.math is not None:
            self.math.append(data)
            return
        if self.dropped:
            return
        if self.pre:
            for index, line in enumerate(data.split("\n")):
                if index:
                    self._break()
                if line:
                    self._write(line)
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip():
            self._write(text if self.out and not self.pending else text.lstrip())
        elif text and self.out and not self.pending:
            self._write(" ")

    def result(self):
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def html_to_markdown(text: str) -> str:
    """Convert exported HTML. Plain text is returned unchanged so newlines survive."""
    if not looks_like_html(text):
        return text if text.endswith("\n") or not text else text + "\n"
    parser = _Markdown()
    parser.feed(text)
    parser.close()
    return parser.result()
