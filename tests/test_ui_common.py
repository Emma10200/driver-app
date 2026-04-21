from __future__ import annotations

from types import SimpleNamespace

import ui.common as common


class FakeSessionState(dict):
    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - defensive parity with Streamlit session state
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value):
        self[key] = value


def test_scroll_to_top_injects_retry_script(monkeypatch):
    captured: dict[str, object] = {}
    fake_state = FakeSessionState()

    def fake_html(markup: str, height: int = 0) -> None:
        captured["markup"] = markup
        captured["height"] = height

    monkeypatch.setattr(common, "st", SimpleNamespace(session_state=fake_state))
    monkeypatch.setattr(common, "components", SimpleNamespace(html=fake_html))

    common.scroll_to_top_on_page_change(7)

    assert fake_state["last_rendered_page"] == 7
    assert captured["height"] == 0
    markup = str(captured["markup"])
    assert '[data-testid="stMain"]' in markup
    assert '[data-testid="stAppViewBlockContainer"]' in markup
    assert '[0, 40, 120, 240]' in markup
    assert 'scrollEverythingToTop' in markup


def test_scroll_to_top_skips_repeat_page(monkeypatch):
    calls: list[str] = []
    fake_state = FakeSessionState(last_rendered_page=7)

    def fake_html(markup: str, height: int = 0) -> None:
        calls.append(markup)

    monkeypatch.setattr(common, "st", SimpleNamespace(session_state=fake_state))
    monkeypatch.setattr(common, "components", SimpleNamespace(html=fake_html))

    common.scroll_to_top_on_page_change(7)

    assert calls == []
    assert fake_state["last_rendered_page"] == 7