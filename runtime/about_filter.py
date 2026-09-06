#!/usr/bin/env python3
"""Render Markdown with repository-relative links pinned to the README commit."""

import os
import posixpath
import sys
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import bleach
import markdown
from markdown.treeprocessors import Treeprocessor
from pygments.formatters import HtmlFormatter

from common import config, git, repository


class RepoLinks(Treeprocessor):
    def __init__(self, md, repo_url, filename, commit):
        super().__init__(md)
        self.repo_url, self.filename, self.commit = repo_url, filename, commit

    def run(self, root):
        for node in root.iter():
            attr = "src" if node.tag == "img" else "href" if node.tag == "a" else None
            if not attr:
                continue
            value = node.get(attr, "")
            url = urlsplit(value)
            if not value or url.scheme or url.netloc or value.startswith(("/", "#")):
                continue
            path = posixpath.normpath(posixpath.join(posixpath.dirname(self.filename), unquote(url.path)))
            if path == ".." or path.startswith("../"):
                continue
            route = "plain" if node.tag == "img" else "tree"
            target = "/" + quote(self.repo_url, safe="/") + "/" + route + "/" + quote(path, safe="/")
            node.set(attr, urlunsplit(("", "", target, urlencode({"id": self.commit}), url.fragment)))
        return root


def main():
    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    repo_url = os.environ.get("CGIT_REPO_URL", "")
    filename = sys.argv[1] if len(sys.argv) > 1 else "README.md"
    md = markdown.Markdown(extensions=["fenced_code", "codehilite", "tables", "sane_lists", "toc"])
    if repo_url:
        repo, _ = repository(config(), repo_url)
        # cgit about pages read configured README refs (normally default branch),
        # not necessarily the branch selected in the page's query string.
        revision, sep, relative = filename.partition(":")
        if sep:
            revision = revision or os.environ.get("CGIT_REPO_DEFBRANCH", "HEAD") or "HEAD"
            filename = relative
        else:
            revision = os.environ.get("CGIT_REPO_DEFBRANCH", "HEAD") or "HEAD"
        result = git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}", check=False)
        if not result.returncode:
            md.treeprocessors.register(RepoLinks(md, repo_url, filename, result.stdout.decode().strip()), "repo-links", 5)
    rendered = md.convert(text)
    tags = set(bleach.sanitizer.ALLOWED_TAGS) | {
        "p", "pre", "code", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "thead", "tbody", "tr", "th", "td", "hr", "br", "img", "del",
        "details", "summary", "kbd", "sup", "sub",
    }
    rendered = bleach.clean(rendered, tags=tags, attributes={
        "*": ["class", "id"], "a": ["href", "title"], "img": ["src", "alt", "title"],
        "th": ["align"], "td": ["align"],
    }, protocols=["http", "https", "mailto"], strip=True)
    print("<style>" + HtmlFormatter(style="pastie").get_style_defs(".codehilite") + "</style>")
    print('<div class="markdown-body">' + rendered + "</div>")


if __name__ == "__main__":
    main()
