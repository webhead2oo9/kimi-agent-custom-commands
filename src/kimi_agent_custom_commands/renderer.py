"""The one renderer used for previews and live command responses."""

from __future__ import annotations

from kimi_agent_module_api.contracts import (
    LayoutGallery,
    LayoutSection,
    LayoutSeparator,
    LayoutText,
    OutgoingLayout,
    SelectSpec,
)

from kimi_agent_custom_commands.models import (
    DividerBlock,
    FieldBlock,
    HeadingBlock,
    ImagesBlock,
    Page,
    SmallBlock,
    TextBlock,
)


def render_page(page: Page) -> OutgoingLayout:
    items: list[LayoutText | LayoutSeparator | LayoutGallery | LayoutSection] = []
    thumbnail_pending = page.thumbnail_url
    index = 0
    while index < len(page.blocks):
        block = page.blocks[index]
        if thumbnail_pending and _text_content(block) is not None:
            texts: list[str] = []
            while index < len(page.blocks) and len(texts) < 3:
                text = _text_content(page.blocks[index])
                if text is None:
                    break
                texts.append(text)
                index += 1
            items.append(LayoutSection(tuple(texts), thumbnail_pending))
            thumbnail_pending = None
            continue
        items.append(_render_block(block))
        index += 1
    return OutgoingLayout(tuple(items), accent_color=page.accent_color)


def _render_block(
    block: HeadingBlock | TextBlock | FieldBlock | DividerBlock | ImagesBlock | SmallBlock,
) -> LayoutText | LayoutSeparator | LayoutGallery:
    text = _text_content(block)
    if text is not None:
        return LayoutText(text)
    if isinstance(block, ImagesBlock):
        return LayoutGallery(block.urls)
    return LayoutSeparator()


def _text_content(
    block: HeadingBlock | TextBlock | FieldBlock | DividerBlock | ImagesBlock | SmallBlock,
) -> str | None:
    if isinstance(block, HeadingBlock):
        return f"## [{block.text}]({block.url})" if block.url else f"## {block.text}"
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, FieldBlock):
        return f"**{block.name}**\n{block.value}"
    if isinstance(block, SmallBlock):
        return f"-# {block.text}"
    return None


def page_selector(command_id: int, pages: tuple[Page, ...], selected: int = 0) -> SelectSpec | None:
    if len(pages) < 2:
        return None
    options = tuple(
        (
            f"✓ {page.label[:98]}" if index == selected else page.label,
            page.key,
            page.description or None,
        )
        for index, page in enumerate(pages)
    )
    return SelectSpec(
        key="command_page",
        options=options,
        placeholder="Choose a page",
        parts=(str(command_id),),
    )


__all__ = ["page_selector", "render_page"]
