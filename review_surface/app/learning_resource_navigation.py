"""Bounded discovery of lesson pages from educational resource directories.

Only links present in a fetched page are followed. A directory is useful discovery
data, but cannot itself satisfy the instructional-source requirement for a lesson.
"""
from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse


def navigation_page(parser, text: str, url: str) -> bool:
    links = (list(parser.main_links) if parser.has_main else
             [href for href in parser.links if href not in parser.navigation_links])
    if len(set(links)) < 3:
        return False
    labels = " ".join(parser.link_labels.get(href, "") for href in links)
    # Ignore link text when judging whether the page actually explains anything.
    prose = text
    for label in sorted(set(parser.link_labels.values()), key=len, reverse=True):
        if label.strip():
            prose = prose.replace(label.strip(), "")
    prose = " ".join(prose.split())
    if parser.has_main:
        # Site-wide menus, forms and repeated footer labels must not make a
        # directory look like a long explanatory article.
        prose = ' '.join(parser.main_prose)
        labels = ' '.join(parser.main_link_text)
    path = urlparse(url).path.lower().rstrip('/')
    directory = not path or bool(re.search(r'(?:^|/)(?:index|contents|menu)[^/]*$|menu\.html?$', path))
    labelled_menu = re.search(r'\b(?:main menu|table of contents)\b|\bmenu\s*$', parser.title, re.I)
    chapter_overview = 'chapter outline' in text.lower() and path.endswith('-introduction')
    if chapter_overview or (labelled_menu and len(prose) < 6000):
        return True
    return len(prose) < 240 or (directory and len(prose) < 1200) or len(labels) > 2 * max(1, len(prose))


def lesson_links(parser, base_url: str, topics: list[str], title: str) -> list[str]:
    stop = {'introduction', 'learn', 'learning', 'tutorial', 'guide', 'chapter',
            'the', 'and', 'for', 'with', 'from', 'www', 'com', 'org', 'html'}
    def tokens(value):
        return {word.rstrip('s') for word in re.findall(r'[a-z]{3,}', value.lower()) if word not in stop}
    wanted = tokens(' '.join(topics) + ' ' + title)
    host = urlparse(base_url).hostname
    scored = {}
    for href in parser.links:
        target = urldefrag(urljoin(base_url, href))[0]
        parsed = urlparse(target)
        if parsed.scheme not in {'http', 'https'} or parsed.hostname != host or parsed.username or parsed.password:
            continue
        if target == urldefrag(base_url)[0] or parsed.query:
            continue
        if re.search(r'\.(?:png|jpe?g|gif|svg|zip|css|js|mp4)$', parsed.path, re.I):
            continue
        label = parser.link_labels.get(href, '')
        if re.search(r'\b(?:login|sign in|register|donate|privacy|contact|copyright|buy)\b', label, re.I):
            continue
        score = len(wanted & tokens(label)) * 3 + len(wanted & tokens(parsed.path))
        if score:
            scored[target] = max(scored.get(target, 0), score)
    return sorted(scored, key=lambda url: (-scored[url], url))
