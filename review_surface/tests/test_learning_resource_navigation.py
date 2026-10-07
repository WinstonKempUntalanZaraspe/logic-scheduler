import asyncio

import pytest

from app import project_intelligence_sources as sources
from app.project_intelligence_models import LearningResource
from app.project_intelligence_quality import lesson_issues, quality_issues
from test_learning_readiness import good


def menu(links):
    return ('<title>Learning resources</title><p>Choose a topic from this educational directory. '
            'These links provide routes to introductory lessons and guided practice.</p>' +
            ''.join(f'<a href="{href}">{label}</a>' for href, label in links) +
            '<a href="/privacy">Privacy</a><a href="/contact">Contact</a>').encode()


LESSON = (b'<title>Understanding atoms</title><p>Atoms contain protons, neutrons and electrons. '
          b'The proton number determines the element. A neutral sodium atom has eleven protons '
          b'and eleven electrons; a sodium ion with positive charge has ten electrons. '
          b'Calculate the charge from the difference between proton and electron counts.</p>')


def test_follows_real_topic_links_through_two_menus_and_rejects_directory(monkeypatch):
    visited = []
    pages = {
        'https://teaching.example/': menu([('/atoms/menu.html', 'Atomic structure and ions')]),
        'https://teaching.example/atoms/menu.html': menu([('/atoms/ions.html', 'Atoms and ions explained')]),
        'https://teaching.example/atoms/ions.html': LESSON,
    }
    async def download(url):
        visited.append(url)
        return url, 'text/html', pages[url]
    monkeypatch.setattr(sources, 'download', download)
    resource = LearningResource(id='atoms', title='Atomic structure', url='https://teaching.example/',
                                topics=['atoms', 'ions'], reason='Foundations')
    result = asyncio.run(sources.fetch_learning_resources([resource]))[0]
    assert result['status'] == 'navigation'
    assert visited == list(pages)
    child, = result['linked_documents']
    assert child['status'] == 'retrieved' and child['url'].endswith('/atoms/ions.html')
    d = good()
    d.learning_resources[0].url = resource.url
    result['id'] = 'python'
    assert any('missing_lesson_resource' in issue for issue in lesson_issues(d, [result]))


def test_detailed_page_with_navigation_is_still_valid_instruction(monkeypatch):
    async def download(url):
        return url, 'text/html', LESSON + b'<p>This lesson supports your chemistry syllabus.</p><nav><a href="/">Home</a><a href="/topics">Topics</a><a href="/about">About</a></nav>'
    monkeypatch.setattr(sources, 'download', download)
    result = asyncio.run(sources.fetch_learning_resources(good().learning_resources))[0]
    assert result['status'] == 'retrieved'
    assert result['material_role'] == 'instruction'


def test_verbose_main_menu_and_chapter_overview_are_not_lessons():
    from app.learning_resource_navigation import navigation_page
    for title, url, prefix in [
        ('Chemistry Main Menu', 'https://teaching.example/', ''),
        ('Chapter 2 Introduction', 'https://teaching.example/2-introduction', 'Chapter Outline'),
    ]:
        parser = sources.PageParser()
        parser.feed(f'<title>{title}</title><h1>{prefix}</h1><p>' +
                    'This section covers atomic properties and provides links to the full teaching pages. ' * 25 +
                    '</p><a href="/atoms">Atoms</a><a href="/ions">Ions</a><a href="/bonds">Bonds</a>')
        assert navigation_page(parser, parser.clean_text(), url)


def test_subject_directory_ignores_long_site_chrome():
    from app.learning_resource_navigation import navigation_page
    parser = sources.PageParser()
    parser.feed('<title>User research - Service Manual</title><header>' + 'Government services and information. ' * 100 +
                '</header><main><h1>User research</h1><p>Plan research and understand user needs.</p>' +
                ''.join(f'<a href="/research/{i}">Detailed user research teaching guide {i}</a>' for i in range(20)) +
                '</main><footer>' + 'Feedback, contact information and services. ' * 100 + '</footer>')
    assert navigation_page(parser, parser.clean_text(), 'https://teaching.example/user-research')


def test_discovery_is_bounded_and_does_not_follow_external_or_private_links(monkeypatch):
    visited = []
    async def download(url):
        visited.append(url)
        return url, 'text/html', menu([
            ('http://127.0.0.1/atoms', 'Atoms'), ('https://elsewhere.example/atoms', 'Atoms'),
            *[(f'/atoms/{len(visited)}/{i}/menu.html', 'Atoms lessons') for i in range(10)]])
    monkeypatch.setattr(sources, 'download', download)
    resource = LearningResource(id='atoms', title='Atoms', url='https://teaching.example/', topics=['atoms'], reason='Basics')
    result = asyncio.run(sources.fetch_learning_resources([resource]))[0]
    assert result['status'] == 'navigation' and result['linked_documents'] == []
    assert 1 < len(visited) <= 5
    assert all(url.startswith('https://teaching.example/') for url in visited)


@pytest.mark.parametrize('verb', ['Practise', 'Practice'])
def test_active_exercises_accept_both_english_spellings(verb):
    d = good()
    d.work_packages[0].description = 'Sodium ion particle counts.'
    d.work_packages[0].exercise = f'{verb} three sodium ion charge questions.'
    assert not any('passive_learning' in issue for issue in quality_issues(d, 'Prepare me from zero knowledge'))


def test_model_can_select_a_discovered_verified_lesson_without_refetch(monkeypatch):
    import json
    from test_learning_readiness import progressive_good, fake_model
    first = progressive_good()
    first.learning_resources[0].url = 'https://teaching.example/'
    repaired = first.model_copy(deep=True)
    repaired.learning_resources[0].id = 'lesson_atoms'
    repaired.learning_resources[0].url = 'https://teaching.example/atoms'
    for package in repaired.work_packages:
        package.resource_ids = ['lesson_atoms']
    calls = fake_model(monkeypatch, [first, repaired])
    fetched = []
    async def fetch(resources, limit=None):
        fetched.extend(r.url for r in resources)
        return [{'id': 'python', 'url': first.learning_resources[0].url, 'status': 'navigation',
                 'linked_documents': [{'id': 'lesson_atoms', 'url': repaired.learning_resources[0].url,
                                       'status': 'retrieved', 'text': LESSON.decode()}]}]
    monkeypatch.setattr(sources, 'fetch_learning_resources', fetch)
    result = asyncio.run(sources.reason_campaign('Prepare me for this hackathon from zero knowledge', []))
    assert fetched == ['https://teaching.example/']
    repair_input = json.loads(calls[1]['input'])
    assert repair_input['verified_linked_learning_documents'][0]['id'] == 'lesson_atoms'
    assert 'linked_documents' not in repair_input['learning_documents'][0]
    assert result._generation['quality_check'] == 'passed'


def test_syllabus_can_be_a_reference_but_not_the_only_teaching_resource(monkeypatch):
    async def download(url):
        return url, 'text/html', b'<title>Examination syllabus</title><p>' + b'Candidates should be able to describe atoms and solve assessed questions. ' * 10 + b'</p>'
    monkeypatch.setattr(sources, 'download', download)
    d = good()
    material = asyncio.run(sources.fetch_learning_resources(d.learning_resources))[0]
    assert material['material_role'] == 'requirements'
    assert material['status'] == 'retrieved'
    issues = lesson_issues(d, [material])
    assert any('missing_lesson_resource:foundation' in issue for issue in issues)
    assert not any('unverified_resource' in issue for issue in issues)
    d.learning_resources.append(LearningResource(id='teacher', title='Taught atoms',
                                                url='https://teaching.example/atoms', topics=['atoms'], reason='Instruction'))
    d.work_packages[0].resource_ids.append('teacher')
    materials = [material, {'id': 'teacher', 'url': d.learning_resources[-1].url, 'status': 'retrieved', 'material_role': 'instruction'}]
    assert not any('missing_lesson_resource:foundation' in issue for issue in lesson_issues(d, materials))


def test_full_exam_can_verify_more_than_twelve_topic_resources(monkeypatch):
    from test_exam_intelligence import exam_ready
    from test_learning_readiness import fake_model
    draft = exam_ready()
    for index in range(12):
        resource = draft.learning_resources[0].model_copy(deep=True)
        resource.id = f'topic_{index}'
        resource.url = f'https://teaching.example/topic-{index}'
        draft.learning_resources.append(resource)
    draft.work_packages[1].resource_ids = ['topic_11']
    calls = fake_model(monkeypatch, [draft])
    result = asyncio.run(sources.reason_campaign('Prepare me for this exam from zero knowledge', []))
    assert len(calls) == 2
    assert len(result._generation['learning_sources']) == 13
