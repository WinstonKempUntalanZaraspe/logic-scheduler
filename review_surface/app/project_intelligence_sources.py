from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import re
import socket
import ssl
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urldefrag

import httpx
from fastapi import HTTPException

from .config import settings
from .project_intelligence_models import CampaignDraft
from .semantic_credentials import semantic_api_key, semantic_model

MAX_SOURCE_BYTES = 8_000_000
MAX_SOURCE_CHARS = 140_000
MAX_PAGES = 10
SPECIFIC_LINK = re.compile(r"\b(?:challenges?|problems?|briefs?|rules?|judging|criteria|rubric|requirements?|submission|syllabus|specification|curriculum|exam(?:ination)?\s*(?:format|scheme)?|past\s*papers?|sample\s*(?:papers?|questions?)|assessment)\b", re.I)
RELEVANT_LINK = re.compile(
    r"\b(?:challenges?|problems?|briefs?|rules?|faq|judg|criteria|rubric|schedule|timeline|dates?|dataset|data|"
    r"submission|deliverable|requirement|technical|technology|resources?|prizes?|eligibility|syllabus|specification|"
    r"curriculum|exam(?:ination)?|paper|past\s*paper|sample\s*question|mark\s*scheme|assessment|formula|archives?)\b", re.I,
)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []
        self.link_labels: dict[str, str] = {}
        self.anchor_href = None
        self.navigation_depth = 0
        self.navigation_links = set()
        self.skip = 0
        self.title = ""
        self.in_title = False
        self.main_depth = 0
        self.has_main = False
        self.main_links = set()
        self.main_prose = []
        self.main_link_text = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == 'main':
            self.main_depth += 1
            self.has_main = True
        if tag in {"nav", "header", "footer"}:
            self.navigation_depth += 1
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(str(href))
                if self.main_depth and not self.navigation_depth:
                    self.main_links.add(str(href))
                if self.navigation_depth: self.navigation_links.add(str(href))
                self.anchor_href = str(href)
        if tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == 'main':
            self.main_depth = max(0, self.main_depth - 1)
        if tag in {"nav", "header", "footer"}:
            self.navigation_depth = max(0, self.navigation_depth-1)
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
        if tag == "a":
            self.anchor_href = None
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.skip:
            return
        value = " ".join(str(data or "").split())
        if not value:
            return
        if self.in_title:
            self.title = (self.title + " " + value).strip()
        if self.anchor_href:
            self.link_labels[self.anchor_href] = self.link_labels.get(self.anchor_href, "") + " " + value
        if self.main_depth and not self.navigation_depth:
            (self.main_link_text if self.anchor_href else self.main_prose).append(value)
        self.text.append(value)

    def clean_text(self) -> str:
        return re.sub(r"[ \t]+", " ", " ".join(self.text)).strip()[:60_000]


def host_is_public(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    except ValueError:
        return True


async def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http/https URLs are supported")
    if not host_is_public(parsed.hostname):
        raise ValueError("Private/local URLs are not allowed")
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), 4,
        )
    except Exception as exc:
        raise ValueError("Could not resolve source website") from exc
    for info in infos:
        if not host_is_public(str(info[4][0]).split("%", 1)[0]):
            raise ValueError("Source website resolves to a private/local address")
    return url


async def download(url: str) -> tuple[str, str, bytes]:
    # Trust both system roots and certifi; never disable certificate validation.
    # Official/competition archive problem packets are often image-heavy PDFs.
    # Keep the normal source cap at 8 MB, but allow a still-bounded 24 MB only
    # for URLs whose path clearly identifies an archive problem/solution PDF.
    path_hint = urlparse(url).path.casefold()
    source_byte_limit = (
        24_000_000
        if re.search(r"/(?:archives?|past[-_/ ]?(?:problems?|papers?))/.*(?:problems?|solutions?)[^/]*\.pdf$", path_hint)
        else MAX_SOURCE_BYTES
    )
    import certifi
    trust = ssl.create_default_context()
    trust.load_verify_locations(certifi.where())

    profiles = [
        (
            {"User-Agent": "AutoScheduler-ProjectIntelligence/1.0",
             "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"},
            httpx.Timeout(15.0, read=25.0),
        ),
        (
            {"User-Agent": "Mozilla/5.0 (compatible; AutoScheduler-ProjectIntelligence/1.0; +https://sgphysicsleague.org)",
             "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"},
            httpx.Timeout(20.0, read=45.0),
        ),
    ]
    last_retryable = None
    for headers, timeout in profiles:
        current = await validate_public_url(url)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, headers=headers, verify=trust
            ) as client:
                for _ in range(5):
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = response.headers.get("location")
                            if not target:
                                raise ValueError("Source website redirected without a destination")
                            current = await validate_public_url(urljoin(current, target))
                            continue
                        response.raise_for_status()
                        declared = int(response.headers.get("content-length") or 0)
                        if declared > source_byte_limit:
                            raise ValueError("Source page is too large")
                        chunks, total = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > source_byte_limit:
                                raise ValueError("Source page is too large")
                            chunks.append(chunk)
                        return current, response.headers.get("content-type", "").lower(), b"".join(chunks)
                raise ValueError("Too many source redirects")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_retryable = exc
            continue
        except httpx.HTTPStatusError as exc:
            # A small set of anti-bot/transient statuses may succeed with the
            # browser-compatible retry. Other client/server errors are definitive.
            if exc.response.status_code in {403, 429, 502, 503, 504}:
                last_retryable = exc
                continue
            raise
    if last_retryable is not None:
        raise ValueError(
            "Source fetch failed after bounded retry (" + type(last_retryable).__name__ + ")"
        ) from last_retryable
    raise ValueError("Source fetch failed")


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF support is not installed") from exc
    reader = PdfReader(io.BytesIO(data))
    parts, total = [], 0
    for page in reader.pages[:60]:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        parts.append(text); total += len(text)
        if total > 120_000:
            break
    return "\n".join(parts)[:120_000]


async def fetch_source_bundle(urls: list[str]) -> tuple[list[dict], list[str]]:
    docs, warnings, seen = [], [], set()
    roots = {urldefrag(x)[0] for x in urls[:4]}
    queue = [(x,0) for x in urls[:4]]
    queued = set(roots)
    root_hosts = {urlparse(x).hostname for x in roots}
    external_specific_queued = 0
    external_specific_limit = 3
    archive_problem_queued = 0
    archive_problem_limit = 2
    archive_year_queued = 0
    archive_year_limit = 1
    specific_entry = all(urlparse(x).path.strip('/') for x in roots)
    while queue and len(docs) < MAX_PAGES and len(seen) < MAX_PAGES * 2:
        requested, depth = queue.pop(0)
        requested = urldefrag(requested)[0]
        if requested in seen:
            continue
        seen.add(requested)
        try:
            final_url, content_type, data = await download(requested)
            root_hosts.add(urlparse(final_url).hostname)
            seen.add(final_url)
            is_pdf = "application/pdf" in content_type or final_url.lower().split("?", 1)[0].endswith(".pdf")
            if is_pdf:
                text = pdf_text(data)
                if text.strip():
                    docs.append({"source": final_url, "source_type": "pdf", "source_role": "primary" if depth == 0 else "supporting", "title": final_url.rsplit("/", 1)[-1], "text": text})
                continue
            charset = "utf-8"
            match = re.search(r"charset=([\w.-]+)", content_type)
            if match:
                charset = match.group(1)
            parser = PageParser(); parser.feed(data.decode(charset, errors="replace"))
            text = parser.clean_text()
            usable = len(text.strip()) >= 150 or bool(re.search(
                r"\b(?:deadline|submission|judging|criteria|requirements?|challenge|technical|must|"
                r"syllabus|specification|curriculum|exam(?:ination)?|assessment|paper|mark\s*scheme|"
                r"topics?|formula|duration|marks?)\b|\d{1,3}%", text, re.I
            ))
            archive_shell_path = bool(re.search(
                r"/(?:archives?|past[-_/ ]?(?:problems?|papers?))(?:/20\d{2})?/?$",
                urlparse(final_url).path,
                re.I,
            ))
            if text and usable:
                docs.append({"source": final_url, "source_type": "website", "source_role": "primary" if depth == 0 else "supporting", "title": parser.title or final_url, "text": text})
            elif text and not archive_shell_path:
                warnings.append(f"Could not extract substantive event information from {final_url}. The page may require JavaScript. Provide a direct challenge/rules page or attach its PDF.")
            # Normally stop at two link-hops. A narrow exception lets an official
            # past-problems archive reached from a rules page expose at most two actual
            # problem/solution documents. This calibrates difficulty without turning
            # source discovery into an unbounded historical crawl.
            archive_index = (
                depth in {0, 1, 2}
                and bool(re.search(r"archives?|past\s+(?:years?\s+)?(?:problems?|papers?)", final_url + " " + parser.title, re.I))
            )
            archive_year_page = (
                depth in {1, 2, 3}
                and bool(re.search(r"/(?:archives?|past[-_/ ]?(?:problems?|papers?))/20\d{2}/?$", urlparse(final_url).path, re.I))
            )
            if depth >= 2 and not (archive_index or archive_year_page):
                continue
            # Some modern event sites render archive cards entirely in JavaScript.
            # If the conventional archive shell exposes no year links in raw HTML,
            # probe only the current-year path. This is URL discovery, not evidence:
            # nothing is trusted unless the fetch succeeds and yields usable content.
            visible_year_links = [
                href for href in parser.links
                if re.search(r"/20\d{2}/?$|\b20\d{2}\b", urlparse(href).path + " " + parser.link_labels.get(href, ""), re.I)
            ]
            if archive_index and not visible_year_links and archive_year_queued < archive_year_limit:
                year = datetime.now(settings.tz).year
                base = final_url if final_url.endswith("/") else final_url + "/"
                candidate = urljoin(base, f"{year}/")
                if candidate not in seen and candidate not in queued:
                    queue.append((candidate, depth + 1))
                    queued.add(candidate)
                    archive_year_queued += 1

            # Likewise, a year page may be a JavaScript shell. Probe only the two
            # conventional problem/solution PDFs, and keep them only if download +
            # PDF extraction succeeds.
            visible_problem_links = [
                href for href in parser.links
                if re.search(r"\b(?:problems?|solutions?)\b", urlparse(href).path + " " + parser.link_labels.get(href, ""), re.I)
            ]
            if archive_year_page and not visible_problem_links:
                base = final_url if final_url.endswith("/") else final_url + "/"
                for name in ("problems.pdf", "solutions.pdf"):
                    if archive_problem_queued >= archive_problem_limit:
                        break
                    candidate = urljoin(base, name)
                    if candidate in seen or candidate in queued:
                        continue
                    queue.append((candidate, depth + 1))
                    queued.add(candidate)
                    archive_problem_queued += 1

            def archive_year_value(href):
                blob = urlparse(href).path + " " + parser.link_labels.get(href, "")
                match = re.search(r"\b(20\d{2})\b", blob)
                return int(match.group(1)) if match else 0
            links = sorted(
                parser.links,
                key=lambda h: (
                    bool(SPECIFIC_LINK.search(urlparse(h).path + ' ' + parser.link_labels.get(h,''))),
                    archive_year_value(h),
                ),
                reverse=True,
            )

            # Some modern event sites render archive buttons client-side, so the
            # visible archive text survives but ordinary <a> links do not. In that
            # narrow case, derive only the newest visible same-site archive year,
            # then only conventional problems/solutions document names when those
            # labels are visibly present. Derived URLs are still untrusted until the
            # normal downloader successfully fetches them.
            if archive_index and not any(re.search(r"/20\d{2}/?$|\b20\d{2}\b",
                                                    urlparse(h).path + " " + parser.link_labels.get(h, ""),
                                                    re.I) for h in links):
                years = sorted({int(y) for y in re.findall(r"\b(20\d{2})\b", text)}, reverse=True)
                if years:
                    links.insert(0, urljoin(final_url.rstrip("/") + "/", f"{years[0]}/"))
            if archive_year_page:
                visible = text.casefold()
                synthetic = []
                base = final_url.rstrip("/") + "/"
                if "problems" in visible and not any("problem" in (urlparse(h).path + " " + parser.link_labels.get(h, "")).casefold() for h in links):
                    synthetic.append(urljoin(base, "problems.pdf"))
                if "solutions" in visible and not any("solution" in (urlparse(h).path + " " + parser.link_labels.get(h, "")).casefold() for h in links):
                    synthetic.append(urljoin(base, "solutions.pdf"))
                links = [*synthetic, *links]

            for href in links:
                candidate = urldefrag(urljoin(final_url, href))[0]
                parsed = urlparse(candidate)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    continue
                label = f"{parsed.path} {parsed.query} {parser.link_labels.get(href, '')}"
                specific = bool(SPECIFIC_LINK.search(label))
                same_source_site = parsed.hostname in root_hosts
                archive_problem = (
                    (archive_index or archive_year_page)
                    and same_source_site
                    and bool(re.search(r"\b(?:problems?|solutions?|past\s*papers?)\b", label, re.I))
                    and archive_problem_queued < archive_problem_limit
                )
                archive_year = (
                    archive_index
                    and same_source_site
                    and not archive_problem
                    and bool(re.search(r"/20\d{2}/?$|\b20\d{2}\b", label, re.I))
                    and archive_year_queued < archive_year_limit
                )
                if archive_index and not (archive_problem or archive_year):
                    continue
                if archive_year_page and not archive_problem:
                    continue
                # Some event sites intentionally delegate their authoritative syllabus,
                # specification or rules to another official organisation (for example a
                # contest page linking the IPhO syllabus). Follow only a clearly labelled
                # one-hop external reference from substantive page content. Never crawl
                # generic external links, navigation/social links, or expand beyond that
                # external document.
                external_specific = (
                    not same_source_site
                    and depth <= 1
                    and specific
                    and (href in parser.main_links or not parser.has_main)
                    and href not in parser.navigation_links
                    and external_specific_queued < external_specific_limit
                )
                if not same_source_site and not external_specific:
                    continue
                # An event's global navigation can point at unrelated conferences or years.
                # Read explicit brief/rules links first; don't expand generic auxiliary pages.
                if specific_entry and href in parser.navigation_links and not specific:
                    continue
                if depth and not specific and not archive_year and not archive_problem:
                    continue
                if (RELEVANT_LINK.search(label) or archive_year or archive_problem) and candidate not in seen and candidate not in queued:
                    queue.append((candidate, 2 if external_specific else depth+1)); queued.add(candidate)
                    if external_specific:
                        external_specific_queued += 1
                    if archive_problem:
                        archive_problem_queued += 1
                    if archive_year:
                        archive_year_queued += 1
        except Exception as exc:
            warnings.append(f"Could not read {requested}: {str(exc)[:160]}")
    return docs, warnings


def response_text(body: dict) -> str:
    return "".join(
        str(part.get("text") or "")
        for item in body.get("output", []) for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


SYSTEM = """You are the Project Intelligence engine above a deterministic daily scheduler.
Prioritize primary documents supplied by the user. Supporting links may describe a different
event or edition on the same domain; do not transfer their dates, eligibility, rubric or
technical requirements unless they explicitly refer to the requested event/edition.
Determine WHAT work should exist for a major goal. Never choose clock times or calendar slots.
Use the supplied source documents and user request. Facts directly supported by a website/PDF or the
user's explicit statement may be VERIFIED. For website/PDF facts, copy the exact source_id supplied with
the supporting document into the output source field (for example source_1); do not invent, rewrite or
summarize the URL. For explicit user facts, use source='user request' and source_type='user_input'.
Anything inferred for preparation must use source_type planner_inference and confidence RECOMMENDED or
OPTIONAL. Never present inference as a competition/site fact. If the user says assume I know nothing,
build only the minimum foundations needed to perform extremely well at this specific goal, not an entire field.

Extract deadlines, presentation dates, rules, requirements, judging/rubric, deliverables, datasets and
technical constraints when supported.

SCOPE SELECTION IS A HARD SAFETY BOUNDARY. Many hackathons/competitions publish multiple mutually
exclusive problem statements, challenge tracks, themes or briefs. Detect and enumerate ALL such candidate
choices in scope_options using stable source-visible keys (for example PS1/PS2/PS3 or a concise slug), title,
summary and provenance. Do not treat ordinary deliverables, judging criteria or sub-parts of one challenge as
separate scope options. If there is exactly one challenge, set selected_scope to that key and
scope_selection_basis='single_option'. If there are multiple choices, select one ONLY when the user's request
itself explicitly names/identifies that choice; then set scope_selection_basis='user_explicit'. If the user has
not explicitly chosen, set selected_scope=null and scope_selection_basis='ambiguous'. NEVER recommend, infer,
randomly choose or silently default to the first problem statement. Never claim the user chose a scope because
it seems to match their skills. In the ambiguous case, do not generate track-specific technical requirements
or pretend one branch is the event; keep any draft work neutral/common because the server will block creation
until the user chooses. Once a scope is explicitly selected, all technical learning/build/testing packages must
match that selected scope plus genuinely common event requirements.

Decompose into concrete work packages with stable short keys
(wp1, wp2...). Dependencies may reference only those keys. Include learning/build/testing/integration/
demo/presentation/rehearsal work when appropriate. Estimates are raw effort minutes, not scheduled time.
For a zero-knowledge learner, name the specific prerequisite concepts and order them before their
applications. For example, a solver challenge may need variables/domains/constraints/objectives and
small modelling exercises; a data challenge needs the relevant data handling, baseline and evaluation.
Do not mechanically give every hackathon the same syllabus. Tie each learning package to a concrete
challenge requirement and a small exercise that demonstrates understanding. Use descriptions for the
actual steps, required resources (only cite supplied links), expected artifact, and self-check.
For exams, treat the verified syllabus/module outline as the coverage boundary.
A named full qualification requires a full curriculum roadmap, not one generic task.
List every main assessable topic in study_topics and connect it to concrete work packages.
Use direct, topic-specific teaching sections for every learning package. A syllabus is
a coverage/assessment reference, never its only teaching resource. Full qualifications
may use up to 24 verified resources so later topics are not forced onto unrelated beginner
pages. When repairing links or stage labels, preserve substantive topic coverage and
honest novice effort; do not shrink preparation time just to shorten the response.
If the syllabus assumes prior knowledge that the user does not have, explicitly add a
prerequisite bridge before the qualification topics. Zero knowledge overrides the
syllabus's assumed learner level. For science practicals include data analysis and
supervised laboratory preparation; do not imply that reading replaces practical skill.
Do not assume the published syllabus year is an exam date or that the exam is tomorrow.
Full qualification readiness can require months: estimate honest novice teaching,
practice, feedback and repeat attempts, including foundations, rather than a compressed
survey course. A short introductory exercise is only the beginning of that roadmap.
 Extract paper duration,
sections, allowed aids, explicit topic/section weights and assessment rules only when supported. Build a
prerequisite-aware topic path, active retrieval, representative questions, cumulative mixed practice,
error correction and timed mocks/past papers. Use mastery/diagnostic evidence when supplied; never invent
a learner weakness or topic weighting. A user statement such as 'I know up to/through X' is a partial
knowledge boundary, not blanket experience: map the syllabus/course prefix through X into
assumed_mastered_topics, put later assessable content in study_topics, and do not create teaching packages
for the mastered prefix unless the user explicitly calls a topic weak/rusty. Known topics may return in
later cumulative or timed practice. Use logical prerequisite/course order, not raw PDF position: an
'assumed knowledge' appendix printed after main content is still prerequisite material and must not imply
the main syllabus is mastered. If syllabus order is ambiguous, do not invent a prefix.
For academic competitions with a published syllabus/specification, use that document as the
coverage boundary and use official past problems, when supplied, to calibrate depth and style.
A learner saying they know a lower-level curriculum (for example O-Level Physics) has a real
baseline, not zero knowledge and not olympiad readiness: skip reteaching genuinely known basics,
then build the missing depth from that baseline through guided contest problems, independent
multi-concept solving and advanced/timed validation. When the competition format gives a timed
round, include format-accurate timed simulation and an error-correction loop. Never invent an
event date merely to make the roadmap concrete; an announced season or "mid-year" window is not
a hard date.

For research use a question, method, evaluation and reproducible
results; for builds use a working baseline, integration and tests.
Keep packages roughly 30-240 minutes where practical; split larger work into measurable checkpoints.
Distinguish required work from optional polish. Preserve the user's prior knowledge, personal time
budget, school, sport and other commitments. If the scope cannot fit, expose the risk and identify a
minimum viable scope; never pretend that effort disappears or sacrifice protected life commitments.
Do not invent rubric percentages when the sources give no numeric weights. Mark inferred weights as
RECOMMENDED. If a source is missing or ambiguous, state the uncertainty in major_risks.
Definition of done must be testable. Use rubric weights and marginal value when setting priority. Do not
invent a deadline. Do not create repetitive daily tasks; a downstream rolling planner exposes 7-14 days.
"""


def _math_exam_fallback_resources(request_text: str, sources: list[dict]):
    """Stable candidate teaching pages for broad mathematics exams.

    These are never treated as syllabus evidence. They are only offered to the model
    after successful retrieval, so repairs can ground lessons even when a generated
    resource URL is dead.
    """
    from .project_intelligence_models import LearningResource
    blob = " ".join([
        str(request_text or ""),
        *(str(item.get("title") or "") + " " + str(item.get("text") or "")[:2000] for item in sources or []),
    ]).lower()
    if not any(token in blob for token in ("math", "mathematics", "algebra", "geometry", "calculus", "trigonometry")):
        return []
    return [
        LearningResource(
            id="fallback_math_numbers",
            title="Math Is Fun — Numbers",
            url="https://www.mathsisfun.com/numbers/index.html",
            topics=["number", "fractions", "ratio", "percentages", "arithmetic"],
            reason="Verified backup teaching resource for foundational number work; never exam-rule evidence.",
        ),
        LearningResource(
            id="fallback_math_algebra",
            title="Math Is Fun — Algebra",
            url="https://www.mathsisfun.com/algebra/index.html",
            topics=["algebra", "equations", "functions", "graphs", "sequences"],
            reason="Verified backup teaching resource for algebraic foundations; never exam-rule evidence.",
        ),
        LearningResource(
            id="fallback_math_geometry",
            title="Math Is Fun — Geometry",
            url="https://www.mathsisfun.com/geometry/index.html",
            topics=["geometry", "mensuration", "trigonometry", "circles", "coordinates", "vectors"],
            reason="Verified backup teaching resource for geometry-related foundations; never exam-rule evidence.",
        ),
        LearningResource(
            id="fallback_math_data",
            title="Math Is Fun — Data",
            url="https://www.mathsisfun.com/data/index.html",
            topics=["statistics", "probability", "data handling"],
            reason="Verified backup teaching resource for statistics and probability; never exam-rule evidence.",
        ),
        LearningResource(
            id="fallback_math_calculus",
            title="Math Is Fun — Calculus",
            url="https://www.mathsisfun.com/calculus/index.html",
            topics=["limits", "differentiation", "integration", "calculus"],
            reason="Verified backup teaching resource for calculus foundations; never exam-rule evidence.",
        ),
    ]


def _chemistry_exam_fallback_resources(request_text: str, sources: list[dict]):
    """Stable candidate teaching pages for broad chemistry exams.

    These are instructional recommendations only. The official syllabus remains the
    authority for assessable content, rules and examination structure. Candidates are
    handed to the model only after the server successfully retrieves usable text.
    """
    from .project_intelligence_models import LearningResource
    blob = " ".join([
        str(request_text or ""),
        *(str(item.get("title") or "") + " " + str(item.get("text") or "")[:2000] for item in sources or []),
    ]).lower()
    if not any(token in blob for token in (
        "chem", "chemistry", "stoichiometry", "atomic structure", "chemical bonding",
        "equilibrium", "organic chemistry", "electrochemistry",
    )):
        return []
    base = "https://openstax.org/books/chemistry-2e/pages/"
    return [
        LearningResource(
            id="fallback_chem_atoms",
            title="OpenStax Chemistry 2e — Atoms, Molecules, and Ions",
            url=base + "2-introduction",
            topics=["atomic structure", "atoms", "ions", "isotopes", "formulae"],
            reason="Verified backup teaching resource for atomic and particle foundations; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_stoichiometry",
            title="OpenStax Chemistry 2e — Stoichiometry of Chemical Reactions",
            url=base + "4-introduction",
            topics=["stoichiometry", "moles", "equations", "limiting reagents", "yield"],
            reason="Verified backup teaching resource for quantitative chemistry; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_structure",
            title="OpenStax Chemistry 2e — Chemical Bonding and Molecular Geometry",
            url=base + "7-introduction",
            topics=["bonding", "structure", "molecular geometry", "intermolecular forces"],
            reason="Verified backup teaching resource for bonding and structure; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_kinetics",
            title="OpenStax Chemistry 2e — Kinetics",
            url=base + "12-introduction",
            topics=["kinetics", "rate", "rate law", "activation energy", "mechanism"],
            reason="Verified backup teaching resource for reaction kinetics; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_equilibrium",
            title="OpenStax Chemistry 2e — Fundamental Equilibrium Concepts",
            url=base + "13-introduction",
            topics=["equilibrium", "equilibrium constant", "Le Chatelier"],
            reason="Verified backup teaching resource for chemical equilibrium; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_acidbase",
            title="OpenStax Chemistry 2e — Acid-Base Equilibria",
            url=base + "14-introduction",
            topics=["acids", "bases", "pH", "buffers", "titration"],
            reason="Verified backup teaching resource for acid-base chemistry; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_redox",
            title="OpenStax Chemistry 2e — Electrochemistry",
            url=base + "17-introduction",
            topics=["redox", "electrochemistry", "cells", "electrolysis"],
            reason="Verified backup teaching resource for oxidation-reduction and electrochemistry; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_transition",
            title="OpenStax Chemistry 2e — Transition Metals and Coordination Chemistry",
            url=base + "19-introduction",
            topics=["transition elements", "coordination chemistry", "complexes"],
            reason="Verified backup teaching resource for transition-metal chemistry; never syllabus-rule evidence.",
        ),
        LearningResource(
            id="fallback_chem_organic",
            title="OpenStax Chemistry 2e — Organic Chemistry",
            url=base + "20-introduction",
            topics=["organic chemistry", "hydrocarbons", "functional groups", "reactions"],
            reason="Verified backup teaching resource for organic chemistry foundations; never syllabus-rule evidence.",
        ),
    ]


def _physics_fallback_resources(request_text: str, sources: list[dict]):
    """Verified instructional backups for broad physics competitions/exams.

    These are teaching aids only. Competition rules/syllabus coverage must still come
    from the event/IPhO sources. The pool is deliberately chapter-level so a repaired
    roadmap can ground mechanics, waves, thermo, E&M, optics and modern physics without
    pretending the user is a zero-knowledge learner.
    """
    from .project_intelligence_models import LearningResource
    blob = " ".join([
        str(request_text or ""),
        *(str(item.get("title") or "") + " " + str(item.get("text") or "")[:2500] for item in sources or []),
    ]).lower()
    if not any(token in blob for token in (
        "physics", "sphl", "ipho", "mechanics", "electromagnet", "thermodynamics",
        "oscillation", "optics", "relativity", "quantum",
    )):
        return []
    return [
        LearningResource(
            id="fallback_physics_measurement",
            title="OpenStax University Physics Vol. 1 — Units and Measurement",
            url="https://openstax.org/books/university-physics-volume-1/pages/1-introduction",
            topics=["measurement", "uncertainty", "accuracy", "precision", "significant figures", "dimensional analysis", "experimental data"],
            reason="Verified backup teaching resource for measurement, uncertainty and experimental-data foundations; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_mechanics",
            title="OpenStax University Physics Vol. 1 — Newton's Laws",
            url="https://openstax.org/books/university-physics-volume-1/pages/5-introduction",
            topics=["mechanics", "forces", "newton laws", "dynamics"],
            reason="Verified backup teaching resource for mechanics above a secondary-school baseline; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_rotation",
            title="OpenStax University Physics Vol. 1 — Fixed-Axis Rotation",
            url="https://openstax.org/books/university-physics-volume-1/pages/10-introduction",
            topics=["rotation", "angular velocity", "torque", "moment of inertia"],
            reason="Verified backup teaching resource for rotational mechanics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_fluids",
            title="OpenStax University Physics Vol. 1 — Fluid Mechanics",
            url="https://openstax.org/books/university-physics-volume-1/pages/14-introduction",
            topics=["fluids", "pressure", "buoyancy", "continuity", "bernoulli"],
            reason="Verified backup teaching resource for fluid mechanics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_waves",
            title="OpenStax University Physics Vol. 1 — Waves",
            url="https://openstax.org/books/university-physics-volume-1/pages/16-introduction",
            topics=["oscillations", "waves", "superposition", "interference", "sound"],
            reason="Verified backup teaching resource for oscillations and waves; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_thermo",
            title="OpenStax University Physics Vol. 2 — First Law of Thermodynamics",
            url="https://openstax.org/books/university-physics-volume-2/pages/3-introduction",
            topics=["thermodynamics", "heat", "work", "internal energy", "ideal gas"],
            reason="Verified backup teaching resource for thermodynamics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_electric",
            title="OpenStax University Physics Vol. 2 — Electric Charges and Fields",
            url="https://openstax.org/books/university-physics-volume-2/pages/5-introduction",
            topics=["electrostatics", "electric field", "coulomb law", "gauss law"],
            reason="Verified backup teaching resource for electrostatics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_magnetism",
            title="OpenStax University Physics Vol. 2 — Magnetic Forces and Fields",
            url="https://openstax.org/books/university-physics-volume-2/pages/11-introduction",
            topics=["magnetism", "lorentz force", "magnetic field", "charged particles"],
            reason="Verified backup teaching resource for magnetism; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_optics",
            title="OpenStax University Physics Vol. 3 — The Nature of Light",
            url="https://openstax.org/books/university-physics-volume-3/pages/1-introduction",
            topics=["optics", "light", "interference", "diffraction", "polarization"],
            reason="Verified backup teaching resource for optics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_relativity",
            title="OpenStax University Physics Vol. 3 — Relativity",
            url="https://openstax.org/books/university-physics-volume-3/pages/5-introduction",
            topics=["relativity", "lorentz transformation", "time dilation", "energy momentum"],
            reason="Verified backup teaching resource for special relativity; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_quantum",
            title="OpenStax University Physics Vol. 3 — Photons and Matter Waves",
            url="https://openstax.org/books/university-physics-volume-3/pages/6-introduction",
            topics=["quantum physics", "photons", "matter waves", "photoelectric effect"],
            reason="Verified backup teaching resource for introductory quantum/modern physics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_nuclear",
            title="OpenStax University Physics Vol. 3 — Nuclear Physics",
            url="https://openstax.org/books/university-physics-volume-3/pages/10-introduction",
            topics=["nuclear physics", "radioactivity", "decay", "fission", "fusion"],
            reason="Verified backup teaching resource for nuclear physics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_math_calculus",
            title="Math Is Fun — Calculus",
            url="https://www.mathsisfun.com/calculus/",
            topics=["calculus", "limits", "derivatives", "differentiation", "integration"],
            reason="Verified backup teaching resource for the mathematical bridge used in olympiad physics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_math_vectors",
            title="Math Is Fun — Scalar, Vector, Matrix",
            url="https://www.mathsisfun.com/algebra/scalar-vector-matrix.html",
            topics=["vectors", "vector", "scalars", "components", "magnitude", "direction"],
            reason="Verified backup teaching resource for vector foundations used in olympiad physics; never competition-rule evidence.",
        ),
        LearningResource(
            id="fallback_physics_math_complex",
            title="Math Is Fun — Complex Numbers",
            url="https://www.mathsisfun.com/numbers/complex-numbers.html",
            topics=["complex numbers", "complex number", "imaginary numbers", "complex plane", "polar form"],
            reason="Verified backup teaching resource for complex-number foundations used in olympiad physics; never competition-rule evidence.",
        ),
    ]


def _exam_fallback_resources(request_text: str, sources: list[dict]):
    """Return subject-specific candidate teaching resources for an exam."""
    resources = [
        *_math_exam_fallback_resources(request_text, sources),
        *_chemistry_exam_fallback_resources(request_text, sources),
        *_physics_fallback_resources(request_text, sources),
    ]
    seen = set()
    unique = []
    for resource in resources:
        key = (resource.id, resource.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resource)
    return unique


async def fetch_learning_resources(resources, limit=12):
    from .learning_resource_navigation import navigation_page, lesson_links
    import hashlib
    semaphore = asyncio.Semaphore(6)

    async def read(url, resource_id, title):
        async with semaphore:
            resolved, kind, data = await download(url)
        parser = None
        if "pdf" in kind or urlparse(resolved).path.lower().endswith('.pdf'):
            text = pdf_text(data)
        else:
            parser = PageParser(); parser.feed(data.decode("utf-8", errors="replace"))
            text = parser.clean_text()
        if len(text.strip()) < 150:
            raise ValueError("No usable instructional text")
        index = parser is not None and navigation_page(parser, text, resolved)
        # A teaching page may mention the syllabus it supports. Classify the
        # document heading, not incidental references inside its explanation.
        heading = parser.title if parser else title + ' ' + text[:600]
        syllabus = bool(re.search(r'\b(?:syllabus|assessment specification|examination specification)\b', heading, re.I))
        return {"id": resource_id, "url": url, "resolved_url": resolved,
                "title": (parser.title if parser else '') or title, "text": text[:12000],
                "material_role": "requirements" if syllabus else "instruction",
                "status": "navigation" if index else "retrieved"}, parser

    async def fetch(resource):
        try:
            result, parser = await read(resource.url, resource.id, resource.title)
            if result['status'] != 'navigation':
                return result
            result['error'] = 'This is a resource directory; choose a verified linked lesson instead.'
            linked, visited = [], {resource.url, result['resolved_url']}
            queue = [(url, 1) for url in lesson_links(parser, result['resolved_url'], resource.topics, resource.title)[:2]]
            # Prioritize descending a relevant directory before a second broad menu.
            # At most four extra downloads and three link levels per proposed resource.
            for _ in range(4):
                if not queue:
                    break
                url, depth = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    rid = 'lesson_' + hashlib.sha256(url.encode()).hexdigest()[:16]
                    child, child_parser = await read(url, rid, resource.title)
                except Exception:
                    continue
                if child['status'] == 'retrieved':
                    linked.append(child)
                elif depth < 3 and child_parser:
                    queue[:0] = [(u, depth + 1) for u in lesson_links(child_parser, child['resolved_url'], resource.topics, resource.title)[:2] if u not in visited]
            result['linked_documents'] = linked
            return result
        except Exception as exc:
            return {"id": resource.id, "url": resource.url, "status": "unavailable",
                    "error": str(exc)[:150]}
    return await asyncio.gather(*(fetch(r) for r in resources[:max(1, int(limit))]))


def strict_schema(model):
    schema = model.model_json_schema()
    def visit(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["required"] = list(value.get("properties", {}))
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    visit(schema)
    return schema



def _event_mechanics_issues(draft: CampaignDraft, source_documents: list[dict]) -> list[str]:
    """Require distinctive source-described competition mechanics to survive synthesis.

    This is intentionally evidence-triggered, not event-name-specific. Generic competitions
    are unaffected. When an official source explicitly describes an unusual interaction or
    scoring mechanic, a preparation blueprint must contain practice for that mechanic rather
    than collapsing the event into generic subject study.
    """
    if draft.campaign_type != "competition":
        return []
    source_blob = " ".join(str(d.get("text") or "") for d in source_documents or []).lower()
    plan_blob = " ".join([
        str(draft.summary or ""),
        *[str(x.value or "") for section in (
            draft.requirements, draft.rules, draft.technical_requirements
        ) for x in section],
        *[
            " ".join([
                str(p.title or ""), str(p.description or ""), str(p.exercise or ""),
                str(p.self_check or ""), str(p.definition_of_done or ""),
            ])
            for p in draft.work_packages
        ],
    ]).lower()
    issues = []

    checks = []
    if "interactive problems" in source_blob and ("simulation quer" in source_blob or "simulation of" in source_blob):
        checks.append((
            ("interactive problem", "simulation quer"),
            "missing_interactive_problem_practice: the official source includes interactive simulation-query problems. "
            "Add a concrete practice package that plans informative queries, interprets returned data, respects query limits, "
            "and solves from the collected evidence."
        ))
    if "half hour rush" in source_blob:
        checks.append((
            ("half hour rush", "hhr"),
            "missing_timed_special_round_strategy: the official source includes a named timed special round/mechanic. "
            "Add format-specific timed practice and decision/coordination strategy for that period."
        ))
    if re.search(r"concurrently\s+access\s+up\s+to\s+4\s+problems|access\s+to\s+4\s+problems", source_blob):
        checks.append((
            ("unlock", "four problems", "4 problems", "concurrent"),
            "missing_unlock_strategy: the official source limits concurrent unlocked problems. "
            "Add practice for unlock/skip selection, parallel problem triage and opportunity-cost decisions."
        ))
    if "submit your workings within 1 hour" in source_blob or (
        "submitting workings" in source_blob and re.search(r"within\s+1\s+hour", source_blob)
    ):
        checks.append((
            ("workings", "rough work", "working submission"),
            "missing_workings_submission_practice: the official source requires post-round workings submission. "
            "Add a rehearsal/checklist for preserving legible derivations, diagrams/code evidence and submitting them within the stated window."
        ))
    if "generative artificial intelligence" in source_blob and re.search(r"prohibited|not allowed", source_blob):
        checks.append((
            ("generative ai", "no ai", "without ai", "ai prohibited", "ai ban"),
            "missing_tool_restriction_simulation: the official competition restricts generative AI. "
            "Include at least one realistic mock performed under the allowed-tool rules, without prohibited assistance."
        ))

    for needles, message in checks:
        if not any(needle in plan_blob for needle in needles):
            issues.append(message)
    return issues


_OWNED_RESOURCE_MARKER = re.compile(
    r"(?i)^(?:(?:physics\s+)?(?:books?|textbooks?|resources?|materials?)\s+i\s+(?:have|own)|"
    r"i\s+(?:have|own)\s+(?:these\s+|the\s+following\s+)?(?:physics\s+)?(?:books?|textbooks?|resources?|materials?))\s*:\s*(.*)$"
)


def extract_user_owned_learning_resources(text: str):
    """Parse an explicit user-owned resource section without inventing metadata.

    Recommended syntax:
      Resources I own:
      - Book title, edition | topics: mechanics, waves, thermodynamics
      - Another title | chapters: electrostatics, magnetism

    Titles are trusted only as resources the user says are available. Topic/chapter
    coverage is trusted only when the user states it; exact page/chapter numbers are
    never inferred here.
    """
    from .project_intelligence_models import LearningResource

    lines = str(text or "").replace("\r\n", "\n").split("\n")
    entries = []
    collecting = False

    def split_inline(value: str):
        # Semicolon is the only inline separator; commas are common inside titles.
        return [part.strip() for part in value.split(";") if part.strip()]

    for raw in lines:
        stripped = raw.strip()
        marker = _OWNED_RESOURCE_MARKER.match(stripped)
        if marker:
            collecting = True
            entries.extend(split_inline(marker.group(1)))
            continue
        if not collecting:
            continue
        if not stripped:
            # Blank line ends an explicit resource block.
            break
        if re.match(r"^[A-Za-z][A-Za-z ]{1,32}:\s*", stripped) and not re.match(
            r"^(?:[-*•]\s*)?(?:topics?|covers?|chapters?|sections?)\s*:", stripped, re.I
        ):
            break
        if re.match(r"^[-*•]\s+", stripped):
            entries.append(re.sub(r"^[-*•]\s+", "", stripped).strip())
        elif entries and re.match(r"^(?:topics?|covers?|chapters?|sections?)\s*:", stripped, re.I):
            entries[-1] += " | " + stripped
        else:
            # A non-bullet planning sentence ends the resource block.
            break

    resources = []
    seen = set()
    for entry in entries[:20]:
        parts = [part.strip() for part in entry.split("|") if part.strip()]
        if not parts:
            continue
        title = parts[0].strip(" -•*")
        if len(title) < 3:
            continue
        topics = []
        details = []
        for part in parts[1:]:
            match = re.match(r"^(topics?|covers?|chapters?|sections?)\s*:\s*(.+)$", part, re.I)
            if match:
                values = [
                    x.strip() for x in re.split(r",|\band\b", match.group(2), flags=re.I)
                    if x.strip()
                ]
                topics.extend(values)
                details.append(f"{match.group(1).lower()}: {match.group(2).strip()}")
            else:
                details.append(part)
        key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:42] or "resource"
        digest = hashlib.sha256(title.casefold().encode()).hexdigest()[:10]
        rid = f"user_owned_{key}_{digest}"
        if rid in seen:
            continue
        seen.add(rid)
        reason = "User explicitly said this resource is owned/available."
        if details:
            reason += " User-supplied metadata: " + "; ".join(details) + "."
        resources.append(LearningResource(
            id=rid,
            title=title,
            url=f"user-owned://{digest}",
            topics=list(dict.fromkeys(topics)),
            reason=reason,
        ))
    return resources


def _user_owned_materials(resources):
    return [{
        "id": resource.id,
        "url": resource.url,
        "title": resource.title,
        "status": "user_owned",
        "material_role": "instruction",
        "text": (
            resource.title + "\n"
            + ("User-supplied topics/chapters: " + ", ".join(resource.topics) if resource.topics else
               "The user owns this instructional resource; no chapter/topic coverage was supplied.")
            + "\n" + resource.reason
        ),
    } for resource in resources]


def _canonicalize_user_owned_learning_resources(draft: CampaignDraft, owned_resources) -> None:
    """Keep user-owned resource metadata exactly at the user's evidence boundary.

    The reasoning model may choose an owned book, but it may not enrich a title-only
    resource with guessed chapters/topics/pages or invent additional user-owned
    pseudo-resources. Canonical user input always wins.
    """
    canonical_by_id = {str(resource.id): resource for resource in owned_resources}
    canonical_by_url = {str(resource.url): resource for resource in owned_resources}
    cleaned = []
    seen = set()
    removed_owned_ids = set()

    for resource in draft.learning_resources:
        rid = str(resource.id)
        url = str(resource.url)
        canonical = canonical_by_id.get(rid) or canonical_by_url.get(url)
        if canonical is not None:
            cid = str(canonical.id)
            if cid not in seen:
                cleaned.append(canonical.model_copy(deep=True))
                seen.add(cid)
            if rid != cid:
                removed_owned_ids.add(rid)
            continue
        if url.startswith("user-owned://"):
            removed_owned_ids.add(rid)
            continue
        if rid not in seen:
            cleaned.append(resource)
            seen.add(rid)

    for resource in owned_resources:
        rid = str(resource.id)
        if rid not in seen:
            cleaned.append(resource.model_copy(deep=True))
            seen.add(rid)

    draft.learning_resources = cleaned
    if removed_owned_ids:
        for package in draft.work_packages:
            package.resource_ids = [
                rid for rid in package.resource_ids
                if str(rid) not in removed_owned_ids
            ]


def _owned_resource_profile(resource) -> tuple[set[str], str | None]:
    """Return hidden routing hints for well-known owned textbooks.

    These hints only help choose among books the user explicitly says they own.
    They are never copied into the visible user-supplied topic metadata and never
    count as official event/syllabus evidence.  They intentionally stay at broad
    subject-domain level: no chapter, section or page numbers are inferred.
    """
    title = re.sub(r"[^a-z0-9]+", " ", str(getattr(resource, "title", "") or "").casefold()).strip()
    profiles = [
        (
            ("stewart" in title and "calculus" in title),
            {
                "calculus", "limits", "continuity", "derivative", "derivatives",
                "differentiation", "integral", "integrals", "integration", "substitution",
                "series", "sequences", "parametric", "polar", "vectors", "multivariable",
                "partial", "multiple", "line", "surface", "differential", "equations",
            },
            "calculus_computational",
        ),
        (
            ("spivak" in title and "calculus" in title),
            {
                "calculus", "limits", "continuity", "derivative", "derivatives",
                "differentiation", "integral", "integrals", "integration", "sequences",
                "series", "proof", "proofs", "theorem", "theorems", "rigorous",
                "epsilon", "delta", "induction", "inequality", "inequalities",
            },
            "calculus_proof",
        ),
        (
            ("apostol" in title and "calculus" in title),
            {
                "calculus", "limits", "continuity", "derivative", "derivatives",
                "differentiation", "integral", "integrals", "integration", "sequences",
                "series", "proof", "proofs", "theorem", "linear", "algebra", "matrix",
                "matrices", "determinant", "determinants", "vectors", "multivariable",
                "differential", "equations",
            },
            "calculus_structural",
        ),
        (
            (
                "mathematical methods" in title
                and "riley" in title
                and "hobson" in title
            ),
            {
                "mathematical", "methods", "vectors", "matrix", "matrices", "linear",
                "algebra", "complex", "calculus", "multivariable", "differential",
                "equations", "ode", "pde", "fourier", "laplace", "transform", "transforms",
                "tensor", "tensors", "special", "functions", "variational", "probability",
                "numerical", "vector", "fields",
            },
            "math_methods_applied",
        ),
        (
            ("kleppner" in title and "kolenkow" in title),
            {
                "mechanics", "kinematics", "dynamics", "force", "forces", "energy",
                "momentum", "rotation", "torque", "angular", "oscillation", "waves",
                "gravitation", "gravity", "central", "relativity",
            },
            "mechanics_foundation",
        ),
        (
            ("david morin" in title or ("morin" in title and "classical mechanics" in title)),
            {
                "mechanics", "kinematics", "dynamics", "force", "forces", "energy",
                "momentum", "rotation", "torque", "angular", "oscillation", "waves",
                "gravitation", "gravity", "central", "lagrangian", "variational",
            },
            "mechanics_problem_solving",
        ),
        (
            ("gregory" in title and "classical mechanics" in title),
            {
                "mechanics", "kinematics", "dynamics", "force", "forces", "energy",
                "momentum", "rotation", "torque", "angular", "rigid", "oscillation",
                "gravitation", "gravity", "central", "lagrangian", "hamiltonian",
            },
            "mechanics_formal",
        ),
        (
            ("serway" in title and "jewett" in title),
            {
                "mechanics", "kinematics", "dynamics", "energy", "momentum", "rotation",
                "waves", "oscillation", "thermodynamics", "thermal", "electricity",
                "electrostatics", "electromagnetism", "magnetism", "circuits", "induction",
                "optics", "relativity", "quantum", "atomic", "nuclear", "modern",
            },
            "general_physics_modern",
        ),
        (
            (
                ("halliday" in title and "resnick" in title and "krane" in title)
                or title.startswith("hrk ")
                or " hrk " in f" {title} "
            ),
            {
                "mechanics", "kinematics", "dynamics", "energy", "momentum", "rotation",
                "waves", "oscillation", "thermodynamics", "thermal", "electricity",
                "electrostatics", "electromagnetism", "magnetism", "circuits", "induction",
                "optics", "relativity", "quantum", "atomic", "nuclear", "modern",
            },
            "general_physics_foundation",
        ),
    ]
    for matched, tokens, role in profiles:
        if matched:
            return set(tokens), role
    return set(), None


def _owned_resource_stage_bonus(role: str | None, stage: str, lesson_tokens: set[str]) -> int:
    """Prefer the right owned book without making any one book mandatory."""
    mechanics = bool(lesson_tokens & {
        "mechanics", "kinematics", "dynamics", "force", "forces", "energy", "momentum",
        "rotation", "torque", "angular", "gravitation", "gravity", "central", "lagrangian",
        "hamiltonian", "rigid",
    })
    modern = bool(lesson_tokens & {"relativity", "quantum", "atomic", "nuclear", "modern"})
    broad_nonmechanics = bool(lesson_tokens & {
        "waves", "thermodynamics", "thermal", "electricity", "electrostatics",
        "electromagnetism", "magnetism", "circuits", "induction", "optics",
    })

    # Math-library roles are deliberately separated by learning purpose.  The user may
    # own several books containing the same word "calculus"; title-level routing should
    # still choose a computational text for technique practice, a proof text for
    # rigorous reasoning, and a mathematical-physics methods text for ODE/PDE/Fourier/
    # complex-analysis style work.  Exact chapter names still require the saved TOC.
    calculus = bool(lesson_tokens & {
        "calculus", "limits", "continuity", "derivative", "derivatives",
        "differentiation", "integral", "integrals", "integration", "multivariable",
        "partial", "multiple", "parametric", "polar", "series", "sequences",
    })
    proof_math = bool(lesson_tokens & {
        "proof", "proofs", "prove", "theorem", "theorems", "rigorous",
        "epsilon", "delta", "induction", "inequality", "inequalities",
    })
    linear_math = bool(lesson_tokens & {
        "linear", "algebra", "matrix", "matrices", "determinant", "determinants",
        "eigenvalue", "eigenvalues", "eigenvector", "eigenvectors",
    })
    applied_methods = bool(lesson_tokens & {
        "ode", "pde", "fourier", "laplace", "transform", "transforms", "tensor",
        "tensors", "complex", "variational", "special", "functions", "field", "fields",
        "boundary", "bessel", "legendre",
    })
    technique_math = bool(lesson_tokens & {
        "technique", "techniques", "compute", "evaluate", "substitution",
        "parts", "fractions",
    })

    if not role:
        return 0

    if role in {"calculus_computational", "calculus_proof", "calculus_structural", "math_methods_applied"}:
        if proof_math:
            if role == "calculus_proof":
                return 14
            if role == "calculus_structural":
                return 10
            if role == "calculus_computational":
                return 2
            return 1
        if applied_methods:
            if role == "math_methods_applied":
                return 15
            if role == "calculus_structural":
                return 6
            if role == "calculus_computational":
                return 3
            return 2
        if linear_math:
            if role == "calculus_structural":
                return 13
            if role == "math_methods_applied":
                return 8
            return 1
        if calculus:
            if technique_math:
                if role == "calculus_computational":
                    return 14
                if role == "calculus_structural":
                    return 6
                if role == "calculus_proof":
                    return 4
                return 2
            if stage in {"foundation", "guided_practice", "unspecified"}:
                if role == "calculus_computational":
                    return 12
                if role == "calculus_structural":
                    return 7
                if role == "calculus_proof":
                    return 6
                return 2
            if role == "calculus_proof":
                return 11
            if role == "calculus_structural":
                return 10
            if role == "calculus_computational":
                return 6
            return 3
        # Don't let a mathematical-methods compendium win a generic math lesson merely
        # because it contains many broad terms.
        if role == "math_methods_applied":
            return 0
        return 2

    if mechanics:
        if role == "mechanics_foundation":
            return 8 if stage in {"foundation", "guided_practice", "unspecified"} else 4
        if role == "mechanics_problem_solving":
            return 9 if stage in {"independent_build", "advanced_validation"} else 5
        if role == "mechanics_formal":
            return 8 if stage == "advanced_validation" else 3
        if role in {"general_physics_foundation", "general_physics_modern"}:
            return 1
    if modern:
        if role == "general_physics_modern":
            return 10
        if role == "general_physics_foundation":
            return 5
    if broad_nonmechanics:
        if role == "general_physics_foundation":
            return 8 if stage in {"foundation", "guided_practice", "unspecified"} else 6
        if role == "general_physics_modern":
            return 6
    if role in {"general_physics_foundation", "general_physics_modern"}:
        return 5
    return 0

def _lesson_resource_tokens(value: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "into", "using", "use", "learn", "learning",
        "practice", "problem", "problems", "physics", "olympiad", "competition", "chapter",
        "introduction", "university", "volume", "section", "examples", "example",
    }
    aliases = {
        "electromagnetic": "electromagnetism", "magnetic": "magnetism",
        "electric": "electricity", "oscillation": "waves", "oscillations": "waves",
        "thermodynamic": "thermodynamics", "relativistic": "relativity",
        "quantum": "quantum", "rotational": "rotation",
    }
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
        token = aliases.get(token, token)
        if len(token) >= 4 and token not in stop:
            tokens.add(token)
    return tokens


def _auto_ground_verified_lessons(draft: CampaignDraft, materials: list[dict],
                                  backup_resources: list, backup_materials: list[dict]) -> list[dict]:
    """Attach already-verified teaching resources when a valid lesson forgot its ID.

    This is a deterministic bookkeeping repair, not curriculum generation. It never
    invents a URL or marks a source verified: candidates must already have been fetched
    successfully, must be instructional (not a syllabus/rules document), and must have
    topical overlap with the lesson. The original lesson content remains model-authored.
    """
    from .project_intelligence_quality import LEARNING

    fetched = {}
    for item in [*(materials or []), *(backup_materials or [])]:
        if item.get("status") not in {"retrieved", "user_owned"} or item.get("material_role") == "requirements":
            continue
        fetched[str(item.get("id") or "")] = item

    definitions = {str(r.id): r for r in draft.learning_resources}
    for resource in backup_resources or []:
        definitions.setdefault(str(resource.id), resource)

    # Only exact id/url pairs that were successfully fetched may be attached.
    candidates = []
    for rid, resource in definitions.items():
        material = fetched.get(rid)
        if not material or str(material.get("url") or "") != str(resource.url):
            continue
        resource_blob = " ".join([
            str(resource.title or ""), " ".join(map(str, resource.topics or [])),
            str(resource.reason or ""),
        ])
        resource_tokens = _lesson_resource_tokens(resource_blob)
        owned_profile_tokens, owned_profile_role = _owned_resource_profile(resource)
        resource_tokens |= owned_profile_tokens
        candidates.append((rid, resource, resource_tokens, owned_profile_role))

    repairs = []
    existing_ids = {str(r.id) for r in draft.learning_resources}

    def scored_candidates(package, lesson_blob, lesson_tokens):
        scored = []
        for rid, resource, resource_tokens, owned_profile_role in candidates:
            phrase_hits = sum(
                1 for topic in resource.topics
                if str(topic).strip() and str(topic).lower() in lesson_blob.lower()
            )
            overlap = len(lesson_tokens & resource_tokens)
            owned = str(resource.url).startswith("user-owned://")

            # Hidden title-level routing is deliberately broad, so a single ambiguous
            # token must not force a specialist owned book into the wrong domain.
            # Example: "matter waves" is quantum physics, not a reason to attach a
            # mechanics specialist merely because its profile contains classical waves.
            mechanics_specialist = owned_profile_role in {
                "mechanics_foundation", "mechanics_problem_solving", "mechanics_formal"
            }
            strongly_nonmechanical = bool(lesson_tokens & {
                "quantum", "photon", "photons", "atomic", "nuclear", "radioactivity",
                "photoelectric", "electrostatics", "electromagnetism", "electricity",
                "magnetism", "circuits", "induction", "thermodynamics", "thermal", "optics",
            })
            # Energy, momentum, forces and even Hamiltonians also occur in quantum
            # and electromagnetic work. Those shared words cannot establish that a
            # classical-mechanics book teaches the nonmechanical concept. Explicit
            # user-supplied TOC coverage can establish that connection.
            explicit_domain_match = any(
                _lesson_resource_tokens(topic) & lesson_tokens & {
                    "quantum", "atomic", "nuclear", "photoelectric", "electrostatics",
                    "electromagnetism", "electricity", "magnetism", "circuits",
                    "induction", "thermodynamics", "thermal", "optics",
                }
                for topic in resource.topics
            )
            if mechanics_specialist and strongly_nonmechanical and not explicit_domain_match:
                continue

            owned_bonus = 40 if owned else 0
            stage_bonus = _owned_resource_stage_bonus(
                owned_profile_role, str(package.learning_stage or "unspecified"), lesson_tokens
            ) if owned else 0
            score = phrase_hits * 8 + overlap * 4 + owned_bonus + stage_bonus
            # Generic owned books still need topical overlap. Recognised textbooks may
            # use broad hidden routing hints, but those hints are never exposed as
            # user-supplied chapter/topic claims.
            if phrase_hits or overlap:
                scored.append((score, phrase_hits, overlap, rid, resource))
        scored.sort(key=lambda row: (-row[0], -row[1], -row[2], row[3]))
        return scored

    def add_reference_note(package, resource, lesson_tokens):
        matched = [
            str(topic) for topic in resource.topics
            if str(topic).lower() in (
                package.title + " " + package.description + " "
                + " ".join(map(str, package.concepts or []))
            ).lower()
        ]
        visible_topic_tokens = _lesson_resource_tokens(" ".join(map(str, resource.topics or [])))
        hidden_tokens, _ = _owned_resource_profile(resource)
        focus = (
            matched[0]
            if matched
            else next(iter(sorted(lesson_tokens & (visible_topic_tokens | hidden_tokens))), "the relevant concept")
        )
        if str(resource.url).startswith("user-owned://"):
            # Availability is user-verified, not content-fetched.  Do not pretend a
            # chapter/section exists when the user supplied only a title.
            note = (
                f" Owned teaching reference: {resource.title}; "
                f"use it as a reference/problem source for this lesson's {focus} work. "
                "No chapter or page is assumed."
            )
        else:
            note = (
                f" Verified teaching reference: {resource.title}; "
                f"use its {focus} section/examples for this lesson."
            )
        if resource.title.lower() not in package.description.lower():
            package.description = package.description.rstrip() + note
        return focus

    for package in draft.work_packages:
        learning_like = package.work_kind == "learning" or (
            package.work_kind == "other" and bool(LEARNING.search(f"{package.phase} {package.title}"))
        )
        guided_practice_like = (
            package.work_kind == "technical_practice"
            and str(package.learning_stage or "") in {
                "guided_practice", "independent_build", "advanced_validation"
            }
            and bool(
                package.concepts
                or str(package.exercise or "").strip()
                or str(package.worked_example or "").strip()
            )
        )
        # Textbooks are useful beyond the first taught lesson. Keep applying the
        # same topical/verification router to substantive technical practice so an
        # owned specialist can support harder work. Deliberately exclude testing/
        # mock packages: those should stay driven by official problems and the
        # user's actual performance, not by forcing a textbook onto every block.
        if not (learning_like or guided_practice_like):
            continue

        lesson_blob = " ".join([
            package.title, package.description, package.phase,
            " ".join(map(str, package.concepts or [])),
            package.worked_example, package.exercise,
        ])
        lesson_tokens = _lesson_resource_tokens(lesson_blob)
        scored = scored_candidates(package, lesson_blob, lesson_tokens)
        eligible_owned_ids = {row[3] for row in scored if str(row[4].url).startswith("user-owned://")}

        valid_existing = [
            rid for rid in package.resource_ids
            if rid in fetched and rid in definitions
            and str(fetched[rid].get("url") or "") == str(definitions[rid].url)
            and (not str(definitions[rid].url).startswith("user-owned://") or rid in eligible_owned_ids)
        ]
        # Also validate the model's existing choice: ownership alone is not evidence
        # that a book covers this lesson. Leave unrelated public references untouched.
        package.resource_ids = [rid for rid in package.resource_ids
                                if rid not in definitions
                                or not str(definitions[rid].url).startswith("user-owned://")
                                or rid in eligible_owned_ids]

        # If the model already selected one of the user's owned resources, preserve it.
        # If it selected only public web material, add a relevant owned book first rather
        # than deleting the verified web source.  The book becomes the preferred study
        # reference and the fetched page remains useful supplementary material.
        if valid_existing:
            if any(str(definitions[rid].url).startswith("user-owned://") for rid in valid_existing):
                continue
            best_owned = next(
                (row for row in scored if str(row[4].url).startswith("user-owned://")),
                None,
            )
            if not best_owned:
                continue
            _, _, _, rid, resource = best_owned
            package.resource_ids = list(dict.fromkeys([rid, *valid_existing]))
            focus = add_reference_note(package, resource, lesson_tokens)
            repairs.append({
                "package_key": package.key,
                "resource_id": rid,
                "resource_url": resource.url,
                "focus": focus,
                "action": "prefer_owned_with_verified_supplement",
            })
            continue

        if not scored:
            continue
        _, _, _, rid, resource = scored[0]

        if rid not in existing_ids:
            draft.learning_resources.append(resource.model_copy(deep=True))
            existing_ids.add(rid)
        package.resource_ids = [rid]
        focus = add_reference_note(package, resource, lesson_tokens)
        repairs.append({
            "package_key": package.key,
            "resource_id": rid,
            "resource_url": resource.url,
            "focus": focus,
            "action": "attach_grounded_resource",
        })
    return repairs


def _reasoning_source_limit(doc: dict, remain: int) -> int:
    """Bound source text while preserving long authoritative support documents."""
    raw_text = str(doc.get("text") or "")
    supporting = doc.get("source_role") == "supporting"
    heading_blob = " ".join([
        str(doc.get("title") or ""), str(doc.get("source") or ""), raw_text[:2200]
    ])
    syllabus_like = bool(re.search(
        r"\b(?:syllabus|curriculum|specification|subject content|examinable)\b",
        heading_blob, re.I,
    ))
    rules_like = bool(re.search(
        r"\b(?:official\s+rules?|competition\s+rules?|rules?|format|guidebook|guidelines?|"
        r"participant\s+guide|competition\s+guide)\b",
        heading_blob, re.I,
    ))
    rich_supporting = supporting and (syllabus_like or rules_like)
    return min(remain, 50000 if rich_supporting else (12000 if supporting else remain))


async def reason_campaign(request_text: str, docs: list[dict]) -> CampaignDraft:
    key, model = semantic_api_key(), semantic_model()
    if not key or not model:
        raise HTTPException(503, "Project Intelligence needs the configured Luna/OpenAI model. No blueprint was created.")

    # Semester-scale Module Library content is persistent and retrieved on demand.
    # Never dump every stored page into one model call: use the exact course map plus
    # a bounded set of relevant/representative chunks, preserving context for official
    # syllabus/rules sources supplied by the user or discovered elsewhere.
    module_docs, module_resources = [], []
    try:
        from .module_library import module_context_for_request, module_learning_resources_for_request
        module_docs = module_context_for_request(request_text, max_chars=115_000)
        module_resources = module_learning_resources_for_request(request_text)
    except Exception:
        # A damaged optional module index must not make unrelated Project Intelligence
        # requests fail. Explicitly uploaded/current sources still remain available.
        module_docs, module_resources = [], []

    combined_docs = [*docs, *module_docs]
    source_chars, sources = 0, []
    for index,doc in enumerate(combined_docs, start=1):
        remain = MAX_SOURCE_CHARS - source_chars
        if remain <= 0:
            break
        raw_text = str(doc.get("text") or "")
        # A linked official syllabus/specification/rules page can be much broader
        # than an event landing page. Keep a larger bounded excerpt so late
        # mechanics and domains are not silently discarded.
        limit = _reasoning_source_limit(doc, remain)
        text = raw_text[:limit]; source_chars += len(text)
        sources.append({
            "source_id": str(doc.get("source_id") or f"source_{index}"),
            "source": doc.get("source"),
            "source_type": doc.get("source_type"),
            "source_role": doc.get("source_role", "primary"),
            "title": doc.get("title"),
            "text": text,
        })
    from datetime import datetime
    from .project_intelligence_quality import (
        QUALITY_INSTRUCTIONS, quality_issues, lesson_issues, exam_knowledge_boundary,
        repair_cross_domain_regressive_dependencies,
    )
    explicit_owned_resources = extract_user_owned_learning_resources(request_text)
    # Persistent owned books are profile-scoped and automatically reused in every
    # future Project Intelligence campaign. Explicit resources in the current
    # request override a saved copy with the same normalized title.
    try:
        from .owned_resource_library import merge_with_explicit_resources
        owned_resources = merge_with_explicit_resources(explicit_owned_resources)
    except Exception:
        # A damaged/temporarily unavailable library must never prevent planning
        # from using resources explicitly supplied in this request.
        owned_resources = explicit_owned_resources
    # Indexed module PDFs are also user-owned instructional resources, but unlike
    # title-only books they include verified headings and exact page ranges extracted
    # from the uploaded files. Keep exact IDs/URLs so the model cannot fabricate pages.
    existing_ids = {str(resource.id) for resource in owned_resources}
    owned_resources.extend(resource for resource in module_resources if str(resource.id) not in existing_ids)
    owned_materials = _user_owned_materials(owned_resources)
    payload = {
        "local_time": datetime.now(settings.tz).isoformat(),
        "user_request": request_text,
        "user_knowledge_boundary": exam_knowledge_boundary(request_text),
        "source_documents": sources,
        "user_owned_learning_resources": [
            {
                "id": resource.id,
                "title": resource.title,
                "topics": resource.topics,
                "reason": resource.reason,
                "availability": "user_owned",
            }
            for resource in owned_resources
        ],
    }
    from .exam_sources import QUALIFICATION
    broad_exam = bool(QUALIFICATION.search(request_text) or re.search(r"\bsyllabus\b", request_text, re.I) or module_resources)
    module_instruction = (
        "\nMODULE LIBRARY: source_documents whose source begins module:// come from PDFs the user uploaded "
        "and indexed persistently. The course-map document is the navigation/coverage boundary for those PDFs. "
        "Use its exact detected headings and page ranges when available. Representative chunks are evidence for "
        "the underlying content, not the complete corpus. Do not conclude a topic is absent merely because it is "
        "not in the retrieved sample. Build coverage from the course map, retrieve/assign the exact indexed PDF "
        "resource for each lesson, and never invent page numbers or headings. For huge modules, split work into "
        "section-level packages and use active recall/problem solving according to learning_mode rather than "
        "creating giant 'read PDF' tasks. Section status/mastery in the course map is learner state: "
        "do not create first-time teaching packages for sections marked mastered unless the user reports them weak; "
        "mastered material may return later in cumulative retrieval or timed practice."
        if module_resources else ""
    )
    request = {
        "model": model, "store": False,
        "instructions": SYSTEM + QUALITY_INSTRUCTIONS + module_instruction + (
            "\nUSER-OWNED LEARNING RESOURCES: If user_owned_learning_resources is non-empty, prefer those "
            "resources when relevant because the user already has access to them. They are instructional aids, "
            "never evidence for event rules. Do not invent chapter numbers, section names or page numbers that "
            "the user did not provide. Use only the user-supplied topic/chapter metadata when naming a specific "
            "section; otherwise refer to the book/resource at title level."
        ),
        "input": json.dumps(payload, ensure_ascii=False),
        "text": {"format": {"type": "json_schema", "name": "campaign_blueprint", "strict": True, "schema": strict_schema(CampaignDraft)}},
        "max_output_tokens": 24000 if broad_exam else 16000,
    }
    timeout = httpx.Timeout(connect=10.0, read=270.0, write=20.0, pool=10.0)
    import time
    started = time.monotonic()
    attempts = []
    materials = []
    material_cache = {
        (item["id"], item["url"]): item
        for item in owned_materials
    }
    grounding_requested = False
    dependency_repairs = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(4):
            try:
                response = await client.post("https://api.openai.com/v1/responses", headers={"Authorization": "Bearer " + key}, json=request)
            except httpx.TimeoutException as exc:
                raise HTTPException(504, "Project Intelligence timed out while preparing the lessons. No blueprint or tasks were created; try interpreting again.") from exc
            if response.status_code >= 400:
                raise HTTPException(502, "Project Intelligence model could not build a validated blueprint. Check model/API availability and try again.")
            body = response.json()
            state = body.get("status")
            incomplete = (body.get("incomplete_details") or {}).get("reason")
            if state == "incomplete" and incomplete == "max_output_tokens" and request['max_output_tokens'] < 32000 and attempt < 3:
                attempts.append({"response_id":body.get("id"),"model":body.get("model") or model,
                                 "quality_issues":["output_limit: full curriculum exceeded response budget"],"usage":body.get("usage") or {}})
                request['max_output_tokens'] = 32000
                request['instructions'] += "\nKeep the complete syllabus coverage, but make prose concise. Use 30-45 substantive packages for a full qualification, with realistic effort and repeat-practice checkpoints. Do not duplicate examples or source metadata."
                continue
            if state != "completed":
                raise HTTPException(502, "Project Intelligence could not finish the curriculum response (" + str(incomplete or state) + "). No blueprint or tasks were created.")
            try:
                draft = CampaignDraft.model_validate_json(response_text(body))
                _canonicalize_user_owned_learning_resources(draft, owned_resources)
                dependency_repairs = repair_cross_domain_regressive_dependencies(draft)
            except Exception as exc:
                from pydantic import ValidationError
                issue = "invalid structured response"
                if isinstance(exc, ValidationError):
                    first = exc.errors(include_input=False)[0]
                    issue = '.'.join(map(str, first.get('loc', ()))) + ': ' + first.get('type', 'validation')
                raise HTTPException(502, "Project Intelligence returned an invalid blueprint (" + issue + "); nothing was saved or created.") from exc
            issues = quality_issues(draft, request_text)
            # Re-verify learning resources after every repair. A repaired draft may add
            # or replace a source; validating it only against attempt 1 would make a
            # legitimate correction impossible to pass. Cache exact (id, URL) pairs so
            # unchanged sources are not downloaded repeatedly.
            # A complete qualification can span more than twelve distinct topics.
            # Do not force later lessons to reuse an unrelated introductory page.
            resource_limit = 24 if draft.campaign_type == "exam" else (16 if draft.campaign_type == "competition" else 6)
            proposed_resources = list(draft.learning_resources)[:resource_limit]
            unfetched = [
                resource for resource in proposed_resources
                if (resource.id, resource.url) not in material_cache
            ]
            if unfetched:
                for item in await fetch_learning_resources(unfetched, limit=resource_limit):
                    material_cache[(item.get("id"), item.get("url"))] = item
                    for child in item.get('linked_documents', []):
                        material_cache[(child['id'], child['url'])] = child
            materials = [
                material_cache[(resource.id, resource.url)]
                for resource in proposed_resources
                if (resource.id, resource.url) in material_cache
            ]

            # Full exam syllabuses often do not contain actual teaching material.
            # When the model's own resource choices are weak/unreachable, pre-verify a
            # subject-specific backup pool. Only successfully retrieved pages are shown
            # to the repair model, and they never count as official syllabus evidence.
            backup_materials = []
            fallback_resources = []
            successful_materials = [item for item in materials if item.get("status") in {"retrieved", "user_owned"}]
            current_lesson_issues = lesson_issues(draft, materials, request_text)
            resource_grounding_issue = any(
                issue.startswith(("missing_learning_materials", "missing_lesson_resource", "unverified_resource"))
                for issue in current_lesson_issues
            )
            if draft.campaign_type in {"exam", "competition"} and (len(successful_materials) < 2 or resource_grounding_issue):
                fallback_resources = (
                    _exam_fallback_resources(request_text, sources)
                    if draft.campaign_type == "exam"
                    else _physics_fallback_resources(request_text, sources)
                )
                fallback_unfetched = [
                    resource for resource in fallback_resources
                    if (resource.id, resource.url) not in material_cache
                ]
                if fallback_unfetched:
                    for item in await fetch_learning_resources(fallback_unfetched, limit=resource_limit):
                        material_cache[(item.get("id"), item.get("url"))] = item
                        for child in item.get('linked_documents', []):
                            material_cache[(child['id'], child['url'])] = child
                backup_materials = [
                    material_cache[(resource.id, resource.url)]
                    for resource in fallback_resources
                    if (resource.id, resource.url) in material_cache
                    and material_cache[(resource.id, resource.url)].get("status") == "retrieved"
                ]

            deterministic_resource_repairs = _auto_ground_verified_lessons(
                draft, materials, fallback_resources, backup_materials
            )
            if deterministic_resource_repairs:
                # Re-evaluate after attaching only resources that were already
                # successfully fetched and topically matched.
                current_lesson_issues = lesson_issues(
                    draft, [*materials, *backup_materials], request_text
                )

            # Discovery results are cached on their parents for reuse, but sending
            # them there AND in a separate list doubled/tripled repair context.
            def compact(item):
                return {k: (v[:8000] if k == 'text' else v) for k, v in item.items()
                        if k != 'linked_documents'}
            payload["learning_documents"] = [compact(item) for item in materials]
            selected_keys = {(item.get('id'), item.get('url')) for item in materials}
            linked = {(child['id'], child['url']): child
                      for item in material_cache.values() for child in item.get('linked_documents', [])}
            payload['verified_linked_learning_documents'] = [compact(child) for key, child in linked.items()
                                                             if key not in selected_keys][:24]
            if backup_materials:
                payload["verified_backup_learning_documents"] = [compact(item) for item in backup_materials
                                                                  if (item['id'], item['url']) not in selected_keys]
            else:
                payload.pop("verified_backup_learning_documents", None)
            if not grounding_requested:
                grounding_requested = True
                issues.append("Ground every lesson in the retrieved learning_documents. Revise the worked examples and selected sections to match the actual instructional text. Remove unavailable resource IDs and URLs. Educational documents are recommendations, never evidence for event rules.")
            if any(item.get('status') == 'navigation' for item in materials):
                issues.append('navigation_resource: resource menus are not lessons. Use relevant verified_linked_learning_documents, copying their exact id/url into learning_resources and naming the taught section; otherwise propose a direct teaching page for verification. Do not attach one broad homepage to unrelated topics.')
            issues += current_lesson_issues
            issues += _event_mechanics_issues(draft, sources)
            if backup_materials and (
                resource_grounding_issue or any(
                    issue.startswith(("missing_learning_materials", "missing_lesson_resource", "unverified_resource"))
                    for issue in issues
                )
            ):
                feedback_code = (
                    "verified_exam_resource_fallback"
                    if draft.campaign_type == "exam"
                    else "verified_learning_resource_fallback"
                )
                issues.append(
                    feedback_code + ": some model-proposed teaching links were unavailable or insufficient. "
                    "Use the successful verified_backup_learning_documents in the next draft: copy their exact "
                    "id/url into learning_resources and connect each relevant learning lesson to those IDs, naming the "
                    "relevant chapter/section in the lesson description. These are recommended teaching aids only, "
                    "never evidence for syllabus/competition rules or event facts."
                )

            attempts.append({"response_id": body.get("id"), "model": body.get("model") or model,
                             "quality_issues": issues, "usage": body.get("usage") or {}})
            if not issues:
                draft._generation = {"provider": "openai", "model": body.get("model") or model,
                    "requested_model": model, "model_used": True, "attempts": len(attempts),
                    "elapsed_seconds": round(time.monotonic()-started, 2),
                    "sources_read": len(sources), "source_characters": source_chars,
                    "quality_check": "passed", "calls": attempts,
                    "learning_sources": [{k:v for k,v in x.items() if k != "text"} for x in [*materials, *backup_materials]],
                    "deterministic_resource_repairs": deterministic_resource_repairs,
                    "deterministic_dependency_repairs": dependency_repairs}
                return draft
            # Bounded resource-grounding pass plus up to two quality corrections. The
            # fourth call only happens when the previous repaired draft still fails the
            # deterministic gates; a passing draft still stops immediately.
            # The rejected draft is data, not another source of VERIFIED user facts.
            request["input"] = json.dumps({**payload, "quality_feedback": issues,
                "draft_to_revise": draft.model_dump(mode="json")}, ensure_ascii=False)
    raise HTTPException(422, "Project plan did not pass learning-readiness checks after resource verification and bounded correction: "
                        + "; ".join(issues[:3]) + ". Nothing was saved or created.")
