from __future__ import annotations

import pytest
from kimi_agent_module_api.contracts import (
    ButtonSpec,
    LayoutGallery,
    LayoutSection,
    LayoutSeparator,
    LayoutText,
    build_custom_id,
)

from kimi_agent_custom_commands.editor import EditorSession, block_modal, editor_components
from kimi_agent_custom_commands.models import (
    CommandDocument,
    DividerBlock,
    FieldBlock,
    HeadingBlock,
    ImagesBlock,
    Page,
    SmallBlock,
    TextBlock,
    ValidationError,
    document_from_dict,
    document_to_dict,
    validate_document,
    validate_identity,
)
from kimi_agent_custom_commands.renderer import page_selector, render_page


def _document() -> CommandDocument:
    return CommandDocument(
        pages=(
            Page(
                "main",
                "Main",
                thumbnail_url="https://example.com/thumb.png",
                accent_color=0x123456,
                blocks=(
                    HeadingBlock(text="Title", url="https://example.com"),
                    TextBlock(text="Body"),
                    FieldBlock(name="Field", value="Value"),
                    DividerBlock(),
                    ImagesBlock(urls=("https://example.com/a.png",)),
                    SmallBlock(text="Fine print"),
                ),
            ),
        )
    )


def test_document_round_trip_and_all_block_renderers() -> None:
    document = document_from_dict(document_to_dict(_document()))
    layout = render_page(document.pages[0])

    assert document == _document()
    assert layout.accent_color == 0x123456
    assert isinstance(layout.items[0], LayoutSection)
    assert layout.items[0].texts == (
        "## [Title](https://example.com)",
        "Body",
        "**Field**\nValue",
    )
    assert isinstance(layout.items[1], LayoutSeparator)
    assert isinstance(layout.items[2], LayoutGallery)
    assert isinstance(layout.items[3], LayoutText)


def test_thumbnail_does_not_inject_selector_metadata() -> None:
    page = Page(
        "main",
        "Selector label",
        description="Selector description",
        thumbnail_url="https://example.com/thumb.png",
        blocks=(ImagesBlock(urls=("https://example.com/a.png",)),),
    )
    layout = render_page(page)
    assert len(layout.items) == 1
    assert isinstance(layout.items[0], LayoutGallery)


def test_rejects_divider_only_and_thumbnail_without_text() -> None:
    with pytest.raises(ValidationError, match="only dividers"):
        validate_document(CommandDocument(pages=(Page("main", "Main", blocks=(DividerBlock(),)),)))
    with pytest.raises(ValidationError, match="thumbnail requires"):
        validate_document(
            CommandDocument(
                pages=(
                    Page(
                        "main",
                        "Main",
                        thumbnail_url="https://example.com/thumb.png",
                        blocks=(ImagesBlock(urls=("https://example.com/image.png",)),),
                    ),
                )
            )
        )


def test_selected_page_label_stays_within_discord_limit() -> None:
    pages = (
        Page("one", "x" * 100, blocks=(TextBlock(text="One"),)),
        Page("two", "Two", blocks=(TextBlock(text="Two"),)),
    )
    selector = page_selector(1, pages)
    assert selector is not None
    assert len(selector.options[0][0]) == 100


def test_block_edit_modal_id_fits_with_maximum_page_key() -> None:
    session = EditorSession.create(
        1,
        2,
        "test",
        "Test",
        CommandDocument(
            pages=(
                Page(
                    "p" * 32,
                    "Main",
                    blocks=tuple(TextBlock(text=str(index)) for index in range(25)),
                ),
            )
        ),
        now=1,
    )
    session.token = "t" * 11
    session.selected_block = 24
    modal = block_modal(session, "text", editing=True)
    delete = next(
        component
        for component in editor_components(session)
        if isinstance(component, ButtonSpec) and component.key == "editor_block_delete"
    )

    assert modal is not None
    assert len(build_custom_id("custom_commands", modal.key, *modal.parts)) == 100
    assert len(build_custom_id("custom_commands", delete.key, *delete.parts)) <= 100


def test_page_budget_is_per_page() -> None:
    page = Page("one", "One", blocks=(TextBlock(text="x" * 3_000),))
    validate_document(
        CommandDocument(pages=(page, Page("two", "Two", blocks=(TextBlock(text="y" * 3_000),))))
    )
    with pytest.raises(ValidationError, match="page text"):
        validate_document(
            CommandDocument(
                pages=(
                    Page("one", "One", blocks=(TextBlock(text="x" * 4_000), SmallBlock(text="y"))),
                )
            )
        )


@pytest.mark.parametrize("name", ["UPPER", "space here", "a" * 33])
def test_invalid_command_names(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_identity(name, "Description")
