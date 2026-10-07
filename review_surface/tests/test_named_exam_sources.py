import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import exam_sources as es
from app import project_intelligence_runtime as runtime
from app.project_intelligence_models import looks_like_project_blueprint_request

CATALOG='''<table><tr><th>Subject Title</th><th>Language Medium</th><th>H1 Subject Code</th><th>H2 Subject Code</th></tr>
<tr><td><a href="https://isomer-user-content.by.gov.sg/334/c/8873_y26_sy.pdf">Chemistry</a> [Revised]</td><td>English</td><td>8873</td><td>-</td></tr>
<tr><td><a href="https://isomer-user-content.by.gov.sg/334/a/9476_y26_sy.pdf">Chemistry</a> [Revised]</td><td>English</td><td>-</td><td>9476</td></tr>
<tr><td><a href="https://isomer-user-content.by.gov.sg/334/b/9729_y26_sy.pdf">Chemistry</a> *</td><td>English</td><td>-</td><td>9729</td></tr>
<tr><td><a href="https://isomer-user-content.by.gov.sg/334/m/9758_y26_sy.pdf">Mathematics</a></td><td>English</td><td>-</td><td>9758</td></tr></table>'''

@pytest.mark.parametrize('prompt',[
 'Prepare me for Singapore A-Level H2 Chemistry from zero knowledge.',
 'Prepare me for Singapore O Level Maths from scratch.',
 'Prepare me for IGCSE Biology.',
 'Prepare me for H2 Physics.',
 'Create a study plan for my A-Level Chemistry.',
])
def test_named_qualifications_route_to_curriculum(prompt):
    assert looks_like_project_blueprint_request(prompt)

@pytest.mark.parametrize('prompt',['Plan my day.','Study H2 Chemistry for 30 minutes.','Add a task to read my Chemistry notes.','My H2 exam is tomorrow.'])
def test_mentions_and_single_tasks_do_not_authorize_a_curriculum(prompt):
    assert not looks_like_project_blueprint_request(prompt)


def test_live_catalog_selection_distinguishes_level_and_revised_edition():
    parser=es.CatalogParser();parser.feed(CATALOG)
    chosen=es.select_catalog(parser,'Prepare me for H2 Chemistry','H2')
    assert chosen['code']=='9476' and chosen['alternative_codes']==['9729']
    assert es.select_catalog(parser,'Prepare me for H2 Chemistry 9729','H2')['code']=='9729'
    assert es.select_catalog(parser,'Prepare me for H1 Chemistry','H1')['code']=='8873'
    assert es.select_catalog(parser,'Prepare me for H2 Maths','H2')['code']=='9758'
    with pytest.raises(HTTPException):es.select_catalog(parser,'Prepare me for A-Level Chemistry')


def test_no_url_request_reads_catalog_then_actual_syllabus(monkeypatch):
    visited=[]
    async def download(url):
        visited.append(url);return url,'text/html',CATALOG.encode()
    async def fetch(urls):
        visited.extend(urls)
        return [{'source':urls[0],'source_type':'pdf','text':'9476 CHEMISTRY SYLLABUS Subject Content. Atomic structure and bonding.'}],[]
    monkeypatch.setattr(es.sources,'download',download)
    monkeypatch.setattr(es.sources,'fetch_source_bundle',fetch)
    docs,warnings,selection=asyncio.run(runtime.resolve_project_sources('Prepare me for Singapore A-Level H2 Chemistry from zero knowledge.'))
    assert len(visited)==2 and visited[0].startswith('https://www.seab.gov.sg/')
    assert visited[1].endswith('9476_y26_sy.pdf') and len(docs)==2
    assert selection['year_basis']=='current_published_year_assumed'
    assert 'not your exam date' in warnings[0]


def test_unreadable_curriculum_does_not_fall_back_to_invented_lessons(monkeypatch):
    async def fail(url):raise ValueError('offline')
    monkeypatch.setattr(es.sources,'download',fail)
    with pytest.raises(HTTPException) as err:
        asyncio.run(runtime.resolve_project_sources('Prepare me for Singapore H2 Chemistry'))
    assert err.value.status_code==422 and 'no tasks' in err.value.detail.lower()


def test_explicit_link_takes_precedence_over_catalog(monkeypatch):
    async def fetch(urls):return [{'source':urls[0],'text':'Custom school syllabus'}],[]
    monkeypatch.setattr(runtime,'fetch_source_bundle',fetch)
    docs,_,selection=asyncio.run(runtime.resolve_project_sources('Prepare me for H2 Chemistry https://school.example/syllabus.pdf'))
    assert docs[0]['source']=='https://school.example/syllabus.pdf' and selection is None


def test_named_exam_without_board_needs_evidence_not_generic_task():
    with pytest.raises(HTTPException) as err:
        asyncio.run(runtime.resolve_project_sources('Prepare me for A-Level Chemistry'))
    assert 'exam board' in err.value.detail


def test_actual_preview_path_uses_discovery_and_stays_read_only(monkeypatch):
    from test_exam_intelligence import exam_ready
    calls=[]
    async def resolve(text):calls.append(('discover',text));return [],[],{'subject':'Chemistry','code':'9476'}
    async def reason(text,docs):calls.append(('reason',text));return exam_ready()
    async def route(c,f):return None,[],[],[],{'route_key':'campaign-test','project_id':'p'}
    monkeypatch.setattr(runtime,'resolve_project_sources',resolve)
    monkeypatch.setattr(runtime,'reason_campaign',reason)
    monkeypatch.setattr(runtime,'structure_and_route',route)
    monkeypatch.setattr(runtime,'new_preview',lambda *a:{'preview_id':'preview','expires_at_epoch':2000000000})
    payload=SimpleNamespace(text='Prepare me for Singapore A-Level H2 Chemistry from zero knowledge.',fallback_project_id=None)
    result=asyncio.run(runtime.preview_project_request(payload))
    assert [x[0] for x in calls]==['discover','reason']
    assert result['tasks']==[] and result['interpreter_mode']=='project-intelligence'
    assert result['project_intelligence']['blueprint']['syllabus_selection']['code']=='9476'


def test_truncated_full_curriculum_gets_one_larger_bounded_response(monkeypatch):
    import json
    from app import project_intelligence_sources as ps
    from test_learning_readiness import progressive_good
    d=progressive_good()
    monkeypatch.setattr(ps,'semantic_api_key',lambda:'test')
    monkeypatch.setattr(ps,'semantic_model',lambda:'test-model')
    async def materials(resources,limit=6):
        return [{'id':r.id,'url':r.url,'status':'retrieved','text':'Worked example'} for r in resources]
    monkeypatch.setattr(ps,'fetch_learning_resources',materials)
    requests=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):
            requests.append(json.loads(json.dumps(kwargs['json'])))
            body=({'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'}} if len(requests)==1 else
                  {'status':'completed','model':'test-model','output':[{'content':[{'type':'output_text','text':d.model_dump_json()}]}]})
            return SimpleNamespace(status_code=200,json=lambda:body)
    monkeypatch.setattr(ps.httpx,'AsyncClient',Client)
    result=asyncio.run(ps.reason_campaign('Prepare me for H2 Chemistry',[]))
    assert [r['max_output_tokens'] for r in requests]==[24000,32000,32000]
    assert result._generation['attempts']==3
