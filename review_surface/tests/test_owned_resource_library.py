import asyncio

from app import owned_resource_library as library
from app.project_intelligence_models import LearningResource


def test_toc_cleaning_preserves_visible_headings_without_page_noise():
    rows = library.clean_toc_entries([
        "Contents",
        "1. Vectors ........ 3",
        "2. Newton's Laws ........ 21",
        "21",
        "2. Newton's Laws ........ 21",
    ])
    assert rows == ["1. Vectors", "2. Newton's Laws"]


def test_saved_library_resources_become_project_learning_resources(monkeypatch):
    monkeypatch.setattr(library, "_load_raw", lambda: {
        "book": {
            "id": "book",
            "title": "Example Mechanics",
            "edition": "2nd edition",
            "toc_entries": ["1 Vectors", "2 Newton's Laws", "3 Energy"],
            "source_files": ["toc-1.jpg"],
            "extraction": "vision",
        }
    })
    resources = library.library_learning_resources()
    assert len(resources) == 1
    resource = resources[0]
    assert resource.title == "Example Mechanics"
    assert resource.url.startswith("user-owned://library/")
    assert "2 Newton's Laws" in resource.topics
    assert "2nd edition" in resource.reason


def test_explicit_resource_overrides_saved_copy_of_same_title(monkeypatch):
    monkeypatch.setattr(library, "library_learning_resources", lambda: [
        LearningResource(
            id="saved",
            title="Example Mechanics",
            url="user-owned://library/saved",
            topics=["Saved TOC"],
            reason="saved",
        ),
        LearningResource(
            id="other",
            title="Other Physics",
            url="user-owned://library/other",
            topics=["Waves"],
            reason="saved",
        ),
    ])
    explicit = LearningResource(
        id="explicit",
        title="Example Mechanics",
        url="user-owned://explicit",
        topics=["User said this now"],
        reason="explicit",
    )
    merged = library.merge_with_explicit_resources([explicit])
    assert [x.id for x in merged] == ["explicit", "other"]


def test_image_import_calls_vision_once_then_saves_structured_toc(monkeypatch):
    calls = []

    async def fake_extract(title, edition, images):
        calls.append((title, edition, len(images)))
        return ["1 Mechanics", "2 Oscillations"]

    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return {"id": "saved", **kwargs}

    monkeypatch.setattr(library, "_extract_image_toc", fake_extract)
    monkeypatch.setattr(library, "save_owned_resource", fake_save)

    result = asyncio.run(library.import_owned_resource_files(
        title="Example",
        edition="3rd",
        files=[
            ("toc1.jpg", "image/jpeg", b"one"),
            ("toc2.jpg", "image/jpeg", b"two"),
        ],
    ))

    assert calls == [("Example", "3rd", 2)]
    assert saved["toc_entries"] == ["1 Mechanics", "2 Oscillations"]
    assert result["id"] == "saved"
