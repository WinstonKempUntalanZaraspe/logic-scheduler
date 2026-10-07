"""Resolve named exams from a live official catalog, without inventing syllabus URLs."""
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from fastapi import HTTPException
from .config import settings
from . import project_intelligence_sources as sources

QUALIFICATION = re.compile(r'\b(?:[ao][\s\-–]?levels?|h[123]|gcse|igcse|psle|ib\s+(?:diploma|hl|sl)|international\s+baccalaureate)\b', re.I)
EXAM_REQUEST = re.compile(r'\b(?:exam(?:ination)?|syllabus|curriculum)\b', re.I)


def normalized(text):
    text = str(text).lower().replace('\u00a0',' ')
    for pattern, replacement in [(r'\ba[ -]?maths?\b','additional mathematics'),(r'\bmaths?\b','mathematics'),(r'\bchem\b','chemistry'),(r'\bbio\b','biology')]:
        text = re.sub(pattern, replacement, text)
    return ' '.join(re.findall(r'[a-z0-9]+', text))


class CatalogParser(HTMLParser):
    """Keep each subject row paired with the H1/H2/H3 column headers and links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headers=[]; self.rows=[]; self.cells=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag=='table': self.headers=[]
        if tag=='tr': self.cells=[]
        if tag in {'td','th'} and self.cells is not None:
            self.cell={'text':'','links':[],'header':tag=='th'}
        if tag=='a' and self.cell is not None:
            href=dict(attrs).get('href')
            if href:self.cell['links'].append(href)
    def handle_data(self, value):
        if self.cell is not None:self.cell['text']+=' '+value
    def handle_endtag(self, tag):
        if tag in {'td','th'} and self.cell is not None:
            self.cell['text']=' '.join(self.cell['text'].split())
            self.cells.append(self.cell); self.cell=None
        if tag=='tr' and self.cells:
            texts=[c['text'] for c in self.cells]
            if any(c['header'] for c in self.cells) or any('subject code' in t.lower() for t in texts):
                self.headers=texts
            else:self.rows.append((list(self.headers),self.cells))
            self.cells=None


def select_catalog(parser, request_text, tier=None):
    request=' '+normalized(request_text)+' '
    requested_code=re.search(r'\b(?:[3-9]\d{3}|[A-Z]\d{3})\b',request_text)
    options=[]
    for headers,cells in parser.rows:
        if not cells:continue
        subject=re.split(r'↗|\[|\*|°|\(opens',cells[0]['text'],maxsplit=1)[0].strip()
        norm=normalized(subject)
        if not norm or ' '+norm+' ' not in request:continue
        for index,cell in enumerate(cells):
            header=headers[index] if index<len(headers) else ''
            if 'code' not in header.lower():continue
            if tier and not re.search(r'\b'+tier+r'\b',header,re.I):continue
            code=re.search(r'\b(?:\d{4}|[A-Z]\d{3})\b',cell['text'])
            if not code or (requested_code and code[0]!=requested_code[0]):continue
            links=cells[0]['links']+cell['links']
            syllabus=[u for u in links if re.search(r'(?:\.pdf(?:\?|$)|syllabus)',u,re.I)]
            if not syllabus:continue
            # Require the code in the official linked asset to avoid cross-column links.
            exact=[u for u in syllabus if code[0].lower() in u.lower()]
            if not exact and len(syllabus)>1:continue
            options.append({'subject':subject,'code':code[0],'url':(exact or syllabus)[0],
                            'tier':tier or next(iter(re.findall(r'\bH[123]\b',header,re.I)),None),
                            'catalog_row':' | '.join(c['text'] for c in cells),
                            'revised':'revised' in cells[0]['text'].lower(),'match_length':len(norm)})
    if not options:
        raise HTTPException(422,'I could not match that subject and level in the official syllabus catalog. Include the subject code or attach/link your syllabus. No tasks were created.')
    longest=max(o['match_length'] for o in options)
    options=[o for o in options if o['match_length']==longest]
    if not tier and len({o.get('tier') for o in options})>1:
        raise HTTPException(422,'Which level are you preparing for: H1, H2 or H3? Include it in your prompt so I can retrieve the correct syllabus. No tasks were created.')
    revised=[o for o in options if o['revised']]
    if len(options)>1 and len(revised)==1 and not requested_code:
        selected=revised[0]
    elif len(options)==1:selected=options[0]
    else:
        raise HTTPException(422,'More than one official syllabus matches. Include your subject code: '+', '.join(o['code'] for o in options)+'. No tasks were created.')
    selected=dict(selected)
    selected['alternative_codes']=[o['code'] for o in options if o['code']!=selected['code']]
    return selected


async def discover_exam_sources(request_text):
    """Return None for non-exams; identified exams must obtain real curriculum evidence."""
    if not (QUALIFICATION.search(request_text) or EXAM_REQUEST.search(request_text)):
        return None
    tier_match=re.search(r'\bH[123]\b',request_text,re.I)
    singapore=bool(re.search(r'\b(?:Singapore|SEAB)\b',request_text,re.I) or tier_match)
    alevel=bool(tier_match or re.search(r'\ba[\s\-–]?levels?\b',request_text,re.I))
    olevel=bool(re.search(r'\bo[\s\-–]?levels?\b',request_text,re.I))
    if not singapore or not (alevel or olevel):
        raise HTTPException(422,'To retrieve the correct curriculum, include the exam board, qualification and subject, or link/attach the official syllabus. Automatic catalog lookup currently supports Singapore A-Level and O-Level. No tasks were created.')
    year_match=re.search(r'\b20\d{2}\b',request_text)
    year=int(year_match[0]) if year_match else datetime.now(settings.tz).year
    level='a' if alevel else 'o'
    candidate='private' if re.search(r'\bprivate\s+candidate',request_text,re.I) else 'school'
    index_url=f'https://www.seab.gov.sg/gce-{level}-level/{level}-level-syllabuses-examined-for-{candidate}-candidates-{year}/'
    try:
        final_url,ctype,data=await sources.download(index_url)
    except Exception as exc:
        raise HTTPException(422,f'I could not retrieve the official {year} syllabus catalog. Link or attach the official syllabus PDF; no curriculum was invented and no tasks were created.') from exc
    if urlparse(final_url).hostname not in {'www.seab.gov.sg','seab.gov.sg'}:
        raise HTTPException(422,'The official catalog redirected outside SEAB. Attach its syllabus PDF; no tasks were created.')
    parser=CatalogParser(); parser.feed(data.decode('utf-8',errors='replace'))
    selected=select_catalog(parser,request_text,tier_match[0].upper() if tier_match else None)
    selected.update(board='SEAB',level=('A-Level' if alevel else 'O-Level'),year=year,index_url=final_url,
                    year_basis='user_supplied' if year_match else 'current_published_year_assumed')
    selected['url']=urljoin(final_url,selected['url'])
    host=urlparse(selected['url']).hostname or ''
    if not (host=='seab.gov.sg' or host.endswith('.seab.gov.sg') or host=='isomer-user-content.by.gov.sg'):
        raise HTTPException(422,'The selected syllabus was not hosted on the official catalog’s approved document host. Attach its PDF; no tasks were created.')
    docs,warnings=await sources.fetch_source_bundle([selected['url']])
    if not docs or not any(selected['code'] in d.get('text','') and re.search(r'syllabus|subject content',d.get('text',''),re.I) for d in docs):
        raise HTTPException(422,'The selected official syllabus could not be read or its subject code could not be verified. Attach its PDF. No tasks were created.')
    docs.append({'source':final_url,'source_type':'website','source_role':'supporting',
                 'title':f'SEAB {year} {selected["level"]} selected catalog row',
                 'text':selected['catalog_row']})
    warnings.append(f'Official syllabus retrieved: {selected["subject"]} {selected.get("tier") or selected["level"]}, code {selected["code"]}, {year}. '+
        ('You did not specify an exam year; this published edition is a visible planning assumption, not your exam date. ' if not year_match else '')+
        ('The catalog also lists '+', '.join(selected['alternative_codes'])+'; specify a code if you need that edition.' if selected['alternative_codes'] else ''))
    return docs,warnings,selected
