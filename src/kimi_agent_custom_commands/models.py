"""Versioned custom-command documents and pure validation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlparse

DOCUMENT_VERSION = 1
COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_PAGES = 25
MAX_BLOCKS = 25
MAX_PAGE_TEXT = 4_000


class ValidationError(ValueError):
    """A command document cannot be rendered safely by Discord."""


@dataclass(frozen=True, slots=True)
class HeadingBlock:
    type: Literal["heading"] = "heading"
    text: str = ""
    url: str | None = None


@dataclass(frozen=True, slots=True)
class TextBlock:
    type: Literal["text"] = "text"
    text: str = ""


@dataclass(frozen=True, slots=True)
class FieldBlock:
    type: Literal["field"] = "field"
    name: str = ""
    value: str = ""


@dataclass(frozen=True, slots=True)
class DividerBlock:
    type: Literal["divider"] = "divider"


@dataclass(frozen=True, slots=True)
class ImagesBlock:
    type: Literal["images"] = "images"
    urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SmallBlock:
    type: Literal["small"] = "small"
    text: str = ""


type Block = HeadingBlock | TextBlock | FieldBlock | DividerBlock | ImagesBlock | SmallBlock


@dataclass(frozen=True, slots=True)
class Page:
    key: str
    label: str
    description: str = ""
    accent_color: int | None = None
    thumbnail_url: str | None = None
    blocks: tuple[Block, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandDocument:
    version: int = DOCUMENT_VERSION
    pages: tuple[Page, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredCommand:
    command_id: int
    guild_id: int
    name: str
    description: str
    document: CommandDocument
    revision: int
    created_by: int
    updated_by: int
    created_at: float
    updated_at: float


def starter_document() -> CommandDocument:
    return CommandDocument(pages=(Page("main", "Main", blocks=(TextBlock(text="New command"),)),))


def validate_identity(name: str, description: str) -> None:
    if not COMMAND_NAME_RE.fullmatch(name):
        raise ValidationError(
            "name must be 1-32 lowercase letters, numbers, hyphens, or underscores"
        )
    if not 1 <= len(description) <= 100:
        raise ValidationError("description must be 1-100 characters")


def validate_document(document: CommandDocument) -> None:
    if document.version != DOCUMENT_VERSION:
        raise ValidationError(f"unsupported document version {document.version}")
    if not 1 <= len(document.pages) <= MAX_PAGES:
        raise ValidationError(f"a command must have 1-{MAX_PAGES} pages")
    keys: set[str] = set()
    for page in document.pages:
        page_text = 0
        has_non_divider = False
        has_text_block = False
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", page.key) or page.key in keys:
            raise ValidationError("page keys must be unique lowercase identifiers")
        keys.add(page.key)
        if not 1 <= len(page.label) <= 100:
            raise ValidationError("page labels must be 1-100 characters")
        if len(page.description) > 100:
            raise ValidationError("page descriptions cannot exceed 100 characters")
        if page.accent_color is not None and not 0 <= page.accent_color <= 0xFFFFFF:
            raise ValidationError("accent colors must be between 0 and 0xFFFFFF")
        if page.thumbnail_url is not None:
            _validate_url(page.thumbnail_url, "thumbnail")
        if not 1 <= len(page.blocks) <= MAX_BLOCKS:
            raise ValidationError(f"each page must have 1-{MAX_BLOCKS} blocks")
        for block in page.blocks:
            page_text += _validate_block(block)
            has_non_divider = has_non_divider or not isinstance(block, DividerBlock)
            has_text_block = has_text_block or isinstance(
                block, HeadingBlock | TextBlock | FieldBlock | SmallBlock
            )
        if not has_non_divider:
            raise ValidationError("a page cannot contain only dividers")
        if page.thumbnail_url is not None and not has_text_block:
            raise ValidationError("a thumbnail requires a heading, text, field, or small block")
        if page_text > MAX_PAGE_TEXT:
            raise ValidationError(f"rendered page text exceeds {MAX_PAGE_TEXT} characters")


def _validate_block(block: Block) -> int:
    if isinstance(block, HeadingBlock):
        if not 1 <= len(block.text) <= 256:
            raise ValidationError("headings must be 1-256 characters")
        if block.url is not None:
            _validate_url(block.url, "heading link")
        return len(block.text) + (len(block.url) + 7 if block.url else 3)
    if isinstance(block, TextBlock):
        if not 1 <= len(block.text) <= 4_000:
            raise ValidationError("text blocks must be 1-4000 characters")
        return len(block.text)
    if isinstance(block, FieldBlock):
        if not 1 <= len(block.name) <= 256 or not 1 <= len(block.value) <= 1_024:
            raise ValidationError(
                "fields require a 1-256 character name and 1-1024 character value"
            )
        return len(block.name) + len(block.value) + 5
    if isinstance(block, ImagesBlock):
        if not 1 <= len(block.urls) <= 10:
            raise ValidationError("image galleries require 1-10 URLs")
        for url in block.urls:
            _validate_url(url, "image")
        return 0
    if isinstance(block, SmallBlock):
        if not 1 <= len(block.text) <= 2_000:
            raise ValidationError("small text blocks must be 1-2000 characters")
        return len(block.text) + 3
    return 0


def _validate_url(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or len(value) > 2_000:
        raise ValidationError(f"{label} must be a valid HTTP(S) URL")


def document_to_dict(document: CommandDocument) -> dict[str, Any]:
    return asdict(document)


def document_from_dict(value: Any) -> CommandDocument:
    if not isinstance(value, dict) or value.get("version") != DOCUMENT_VERSION:
        raise ValidationError("invalid or unsupported command document")
    raw_pages = value.get("pages")
    if not isinstance(raw_pages, list | tuple):
        raise ValidationError("command pages must be a list")
    pages: list[Page] = []
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict) or not isinstance(raw_page.get("blocks"), list | tuple):
            raise ValidationError("invalid command page")
        blocks = tuple(_block_from_dict(item) for item in raw_page["blocks"])
        color = raw_page.get("accent_color")
        pages.append(
            Page(
                key=_string(raw_page.get("key")),
                label=_string(raw_page.get("label")),
                description=_string(raw_page.get("description", "")),
                accent_color=color
                if isinstance(color, int) and not isinstance(color, bool)
                else None,
                thumbnail_url=_optional_string(raw_page.get("thumbnail_url")),
                blocks=blocks,
            )
        )
    document = CommandDocument(version=DOCUMENT_VERSION, pages=tuple(pages))
    validate_document(document)
    return document


def _block_from_dict(value: Any) -> Block:
    if not isinstance(value, dict):
        raise ValidationError("invalid command block")
    block_type = value.get("type")
    if block_type == "heading":
        return HeadingBlock(text=_string(value.get("text")), url=_optional_string(value.get("url")))
    if block_type == "text":
        return TextBlock(text=_string(value.get("text")))
    if block_type == "field":
        return FieldBlock(name=_string(value.get("name")), value=_string(value.get("value")))
    if block_type == "divider":
        return DividerBlock()
    if block_type == "images":
        urls = value.get("urls")
        if not isinstance(urls, list | tuple):
            raise ValidationError("image URLs must be a list")
        return ImagesBlock(urls=tuple(_string(item) for item in urls))
    if block_type == "small":
        return SmallBlock(text=_string(value.get("text")))
    raise ValidationError(f"unknown block type {block_type!r}")


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("expected text")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _string(value)


__all__ = [
    "Block",
    "CommandDocument",
    "DividerBlock",
    "FieldBlock",
    "HeadingBlock",
    "ImagesBlock",
    "Page",
    "SmallBlock",
    "StoredCommand",
    "TextBlock",
    "ValidationError",
    "document_from_dict",
    "document_to_dict",
    "starter_document",
    "validate_document",
    "validate_identity",
]
