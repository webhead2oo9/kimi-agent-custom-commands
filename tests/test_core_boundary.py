from __future__ import annotations

import pytest

from kimi_agent_custom_commands.editor import EditorSession, editor_components, editor_layout
from kimi_agent_custom_commands.models import CommandDocument, Page, TextBlock


def test_max_editor_preview_passes_real_core_layout_boundaries() -> None:
    module_interactions = pytest.importorskip("discord_adapter.module_interactions")
    document = CommandDocument(
        pages=(
            Page(
                "main",
                "Main",
                blocks=tuple(TextBlock(text=f"{index}: " + "x" * 145) for index in range(25)),
            ),
        )
    )
    session = EditorSession.create(1, 2, "test", "Test", document, now=1)
    session.dirty = True

    layout = editor_layout(session)
    view = module_interactions.build_layout_view(
        layout, editor_components(session), "custom_commands"
    )

    rows = [item for item in view.children if type(item).__name__ == "ActionRow"]
    assert len(rows) <= 5
    assert view.total_children_count <= 40
    assert sum(len(item.content) for item in layout.items if hasattr(item, "content")) <= 4_000
    assert any(
        "Preview truncated" in item.content for item in layout.items if hasattr(item, "content")
    )
