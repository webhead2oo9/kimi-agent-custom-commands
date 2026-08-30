"""Short-lived staff editor sessions and component/modal descriptions."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass, replace

from kimi_agent_module_api.contracts import (
    ButtonSpec,
    LayoutItem,
    ModalSpec,
    OutgoingLayout,
    SelectSpec,
    TextInputSpec,
)

from kimi_agent_custom_commands.models import (
    Block,
    CommandDocument,
    DividerBlock,
    FieldBlock,
    HeadingBlock,
    ImagesBlock,
    Page,
    SmallBlock,
    StoredCommand,
    TextBlock,
)
from kimi_agent_custom_commands.renderer import render_page

SESSION_TTL_SECONDS = 30 * 60
BLOCK_TYPES = ("heading", "text", "field", "divider", "images", "small")
MAX_EDITOR_PREVIEW_ITEMS = 12
MAX_EDITOR_PREVIEW_TEXT = 3_500


@dataclass(slots=True)
class EditorSession:
    token: str
    guild_id: int
    user_id: int
    command_id: int | None
    baseline_revision: int | None
    name: str
    description: str
    document: CommandDocument
    selected_page: int
    selected_block: int
    touched_at: float
    dirty: bool = False

    @classmethod
    def create(
        cls,
        guild_id: int,
        user_id: int,
        name: str,
        description: str,
        document: CommandDocument,
        *,
        original: StoredCommand | None = None,
        now: float | None = None,
    ) -> EditorSession:
        return cls(
            token=secrets.token_urlsafe(8),
            guild_id=guild_id,
            user_id=user_id,
            command_id=original.command_id if original else None,
            baseline_revision=original.revision if original else None,
            name=name,
            description=description,
            document=document,
            selected_page=0,
            selected_block=0,
            touched_at=time.time() if now is None else now,
        )

    @property
    def page(self) -> Page:
        return self.document.pages[self.selected_page]

    def touch(self, now: float) -> None:
        self.touched_at = now

    def set_page(self, index: int) -> None:
        self.selected_page = max(0, min(index, len(self.document.pages) - 1))
        self.selected_block = min(self.selected_block, len(self.page.blocks) - 1)

    def replace_page(self, page: Page) -> None:
        pages = list(self.document.pages)
        pages[self.selected_page] = page
        self.document = replace(self.document, pages=tuple(pages))
        self.dirty = True

    def replace_block(self, block: Block, *, index: int | None = None) -> None:
        blocks = list(self.page.blocks)
        target = self.selected_block if index is None else index
        if target == len(blocks):
            blocks.append(block)
        else:
            blocks[target] = block
        self.replace_page(replace(self.page, blocks=tuple(blocks)))
        self.selected_block = target


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, EditorSession] = {}

    def add(self, session: EditorSession) -> None:
        self._sessions[session.token] = session

    def get(self, token: str, *, guild_id: int, user_id: int, now: float) -> EditorSession | None:
        self.prune(now)
        session = self._sessions.get(token)
        if session is None or session.guild_id != guild_id or session.user_id != user_id:
            return None
        session.touch(now)
        return session

    def remove(self, token: str) -> None:
        self._sessions.pop(token, None)

    def prune(self, now: float) -> None:
        expired = [
            token
            for token, item in self._sessions.items()
            if now - item.touched_at > SESSION_TTL_SECONDS
        ]
        for token in expired:
            self._sessions.pop(token, None)


def editor_layout(session: EditorSession) -> OutgoingLayout:
    preview = render_page(session.page)
    state = "unsaved changes" if session.dirty else "saved state"
    # LayoutText is always accepted as the editor header, including image-only pages.
    from kimi_agent_module_api.contracts import LayoutSeparator, LayoutText

    header = LayoutText(f"**Editing /{session.name}** — {state}")
    preview_items, truncated = _bounded_preview(preview.items, len(header.content))
    items: tuple[LayoutItem, ...] = (header, LayoutSeparator(), *preview_items)
    if truncated:
        items = (*items, LayoutText("-# Preview truncated. The saved command keeps all content."))
    return OutgoingLayout(items, accent_color=preview.accent_color)


def _bounded_preview(
    items: tuple[LayoutItem, ...], used_text: int
) -> tuple[tuple[LayoutItem, ...], bool]:
    from kimi_agent_module_api.contracts import LayoutSection, LayoutText

    bounded: list[LayoutItem] = []
    truncated = False
    for item in items:
        if len(bounded) >= MAX_EDITOR_PREVIEW_ITEMS:
            truncated = True
            break
        if isinstance(item, LayoutText):
            remaining = MAX_EDITOR_PREVIEW_TEXT - used_text
            if remaining <= 0:
                truncated = True
                break
            content = item.content[:remaining]
            bounded.append(LayoutText(content))
            used_text += len(content)
            if len(content) < len(item.content):
                truncated = True
                break
        elif isinstance(item, LayoutSection):
            texts: list[str] = []
            for text in item.texts:
                remaining = MAX_EDITOR_PREVIEW_TEXT - used_text
                if remaining <= 0:
                    truncated = True
                    break
                content = text[:remaining]
                texts.append(content)
                used_text += len(content)
                if len(content) < len(text):
                    truncated = True
                    break
            if texts:
                bounded.append(LayoutSection(tuple(texts), item.thumbnail_url))
            if truncated:
                break
        else:
            bounded.append(item)
    if len(bounded) < len(items):
        truncated = True
    return tuple(bounded), truncated


def editor_components(session: EditorSession) -> tuple[object, ...]:
    token = session.token
    page_options = tuple(
        (page.label, str(index), page.description or None)
        for index, page in enumerate(session.document.pages)
    )
    block_options = tuple(
        (block_label(block), str(index), None) for index, block in enumerate(session.page.blocks)
    )
    add_options = tuple((kind.title(), kind, None) for kind in BLOCK_TYPES)
    components: list[object] = [
        SelectSpec("editor_page", page_options, "Select a page", (token,)),
        SelectSpec("editor_block", block_options, "Select a block", (token, session.page.key)),
        SelectSpec("editor_add_block", add_options, "Add a block", (token, session.page.key)),
        ButtonSpec("editor_general", "General", parts=(token,)),
        ButtonSpec("editor_page_edit", "Edit page", parts=(token, session.page.key)),
        ButtonSpec("editor_page_add", "Add page", parts=(token,)),
        ButtonSpec(
            "editor_page_delete",
            "Delete page",
            style="danger",
            parts=(token, session.page.key),
            disabled=len(session.document.pages) == 1,
        ),
        ButtonSpec(
            "editor_block_edit",
            "Edit block",
            parts=(token, session.page.key, str(session.selected_block)),
        ),
        ButtonSpec(
            "editor_reorder",
            "Reorder",
            parts=(token, session.page.key, str(session.selected_block)),
        ),
        ButtonSpec(
            "editor_block_delete",
            "Delete block",
            style="danger",
            parts=(
                token,
                session.page.key,
                str(session.selected_block),
                block_fingerprint(session.page.blocks[session.selected_block]),
            ),
            disabled=len(session.page.blocks) == 1,
        ),
        ButtonSpec(
            "editor_save",
            "Save",
            style="success",
            parts=(token,),
            disabled=not session.dirty and session.command_id is not None,
        ),
        ButtonSpec("editor_discard", "Discard", parts=(token,)),
        ButtonSpec("editor_close", "Close", style="danger", parts=(token,)),
    ]
    return tuple(components)


def general_modal(session: EditorSession) -> ModalSpec:
    return ModalSpec(
        "editor_modal",
        "Command details",
        (
            TextInputSpec("name", "Name", default=session.name, min_length=1, max_length=32),
            TextInputSpec(
                "description",
                "Description",
                default=session.description,
                min_length=1,
                max_length=100,
            ),
        ),
        (session.token, "general"),
    )


def page_modal(session: EditorSession, *, adding: bool = False) -> ModalSpec:
    page = Page("page", "Page", blocks=(TextBlock(text="New page"),)) if adding else session.page
    return ModalSpec(
        "editor_modal",
        "Add page" if adding else "Edit page",
        (
            TextInputSpec("key", "Page key", default=page.key, min_length=1, max_length=32),
            TextInputSpec(
                "label", "Selector label", default=page.label, min_length=1, max_length=100
            ),
            TextInputSpec(
                "description",
                "Selector description",
                default=page.description,
                required=False,
                max_length=100,
            ),
            TextInputSpec(
                "accent_color",
                "Accent color (hex)",
                default=f"#{page.accent_color:06x}" if page.accent_color is not None else "",
                required=False,
                max_length=7,
            ),
            TextInputSpec(
                "thumbnail_url",
                "Thumbnail URL",
                default=page.thumbnail_url or "",
                required=False,
                max_length=2000,
            ),
        ),
        (session.token, "page_add" if adding else "page_edit", page.key),
    )


def block_modal(session: EditorSession, block_type: str, *, editing: bool) -> ModalSpec | None:
    block = session.page.blocks[session.selected_block] if editing else None
    fingerprint = block_fingerprint(block) if editing and block is not None else "new"
    parts = (
        (
            session.token,
            "block_edit",
            session.page.key,
            str(session.selected_block),
            fingerprint,
        )
        if editing
        else (
            session.token,
            "block_add",
            session.page.key,
            str(session.selected_block),
            block_type,
            fingerprint,
        )
    )
    if block_type == "divider":
        return None
    inputs: tuple[TextInputSpec, ...]
    if block_type == "heading":
        heading = block if isinstance(block, HeadingBlock) else HeadingBlock()
        inputs = (
            TextInputSpec("text", "Heading", default=heading.text, max_length=256),
            TextInputSpec(
                "url", "Optional link", default=heading.url or "", required=False, max_length=2000
            ),
        )
    elif block_type == "text":
        text = block if isinstance(block, TextBlock) else TextBlock()
        inputs = (
            TextInputSpec("text", "Text", style="paragraph", default=text.text, max_length=4000),
        )
    elif block_type == "field":
        field = block if isinstance(block, FieldBlock) else FieldBlock()
        inputs = (
            TextInputSpec("name", "Field name", default=field.name, max_length=256),
            TextInputSpec(
                "value", "Field value", style="paragraph", default=field.value, max_length=1024
            ),
        )
    elif block_type == "images":
        images = block if isinstance(block, ImagesBlock) else ImagesBlock()
        inputs = (
            TextInputSpec(
                "urls",
                "Image URLs, one per line",
                style="paragraph",
                default="\n".join(images.urls),
                max_length=4000,
            ),
        )
    elif block_type == "small":
        small = block if isinstance(block, SmallBlock) else SmallBlock()
        inputs = (
            TextInputSpec(
                "text", "Small text", style="paragraph", default=small.text, max_length=2000
            ),
        )
    else:
        raise ValueError(f"unknown block type {block_type!r}")
    return ModalSpec("editor_modal", f"{'Edit' if editing else 'Add'} {block_type}", inputs, parts)


def reorder_modal(session: EditorSession) -> ModalSpec:
    return ModalSpec(
        "editor_modal",
        "Reorder page or block",
        (
            TextInputSpec(
                "page_position",
                f"Page position (1-{len(session.document.pages)})",
                default=str(session.selected_page + 1),
                min_length=1,
                max_length=2,
            ),
            TextInputSpec(
                "block_position",
                f"Block position (1-{len(session.page.blocks)})",
                default=str(session.selected_block + 1),
                min_length=1,
                max_length=2,
            ),
        ),
        (
            session.token,
            "reorder",
            session.page.key,
            str(session.selected_block),
            block_fingerprint(session.page.blocks[session.selected_block]),
        ),
    )


def block_fingerprint(block: Block) -> str:
    payload = json.dumps(asdict(block), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


def block_from_values(block_type: str, values: dict[str, str]) -> Block:
    if block_type == "heading":
        return HeadingBlock(
            text=values.get("text", "").strip(), url=values.get("url", "").strip() or None
        )
    if block_type == "text":
        return TextBlock(text=values.get("text", "").strip())
    if block_type == "field":
        return FieldBlock(
            name=values.get("name", "").strip(), value=values.get("value", "").strip()
        )
    if block_type == "divider":
        return DividerBlock()
    if block_type == "images":
        return ImagesBlock(
            urls=tuple(line.strip() for line in values.get("urls", "").splitlines() if line.strip())
        )
    if block_type == "small":
        return SmallBlock(text=values.get("text", "").strip())
    raise ValueError(f"unknown block type {block_type!r}")


def block_label(block: Block) -> str:
    if isinstance(block, HeadingBlock):
        return f"Heading: {block.text[:70]}"
    if isinstance(block, TextBlock):
        return f"Text: {block.text[:74]}"
    if isinstance(block, FieldBlock):
        return f"Field: {block.name[:73]}"
    if isinstance(block, ImagesBlock):
        return f"Images ({len(block.urls)})"
    if isinstance(block, SmallBlock):
        return f"Small: {block.text[:73]}"
    return "Divider"


__all__ = [
    "BLOCK_TYPES",
    "EditorSession",
    "SessionStore",
    "block_fingerprint",
    "block_from_values",
    "block_modal",
    "editor_components",
    "editor_layout",
    "general_modal",
    "page_modal",
    "reorder_modal",
]
