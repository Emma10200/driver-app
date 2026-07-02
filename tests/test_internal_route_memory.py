from __future__ import annotations

from types import SimpleNamespace

import pytest

from services import internal_route_memory as route_memory


class RerunCalled(RuntimeError):
    pass


def _fake_st(*, logged_in: bool = False):
    return SimpleNamespace(
        user=SimpleNamespace(is_logged_in=logged_in),
        session_state={},
        query_params={},
        rerun=lambda: (_ for _ in ()).throw(RerunCalled()),
    )


def test_remember_internal_route_only_while_logged_out(monkeypatch):
    fake_st = _fake_st(logged_in=False)
    monkeypatch.setattr(route_memory, "st", fake_st)

    route_memory.remember_internal_route_for_login("gps-map")

    assert fake_st.session_state[route_memory.INTERNAL_ROUTE_SESSION_KEY] == "gps-map"

    logged_in_st = _fake_st(logged_in=True)
    monkeypatch.setattr(route_memory, "st", logged_in_st)

    route_memory.remember_internal_route_for_login("dispatch-board")

    assert route_memory.INTERNAL_ROUTE_SESSION_KEY not in logged_in_st.session_state


def test_restore_internal_route_after_login_sets_query_and_clears_memory(monkeypatch):
    fake_st = _fake_st(logged_in=True)
    fake_st.session_state[route_memory.INTERNAL_ROUTE_SESSION_KEY] = "dispatch-board"
    monkeypatch.setattr(route_memory, "st", fake_st)

    with pytest.raises(RerunCalled):
        route_memory.restore_internal_route_after_login(current_route_requested=False)

    assert fake_st.query_params == {"route": "dispatch-board"}
    assert route_memory.INTERNAL_ROUTE_SESSION_KEY not in fake_st.session_state


def test_restore_internal_route_does_nothing_when_route_already_requested(monkeypatch):
    fake_st = _fake_st(logged_in=True)
    fake_st.session_state[route_memory.INTERNAL_ROUTE_SESSION_KEY] = "qbo"
    monkeypatch.setattr(route_memory, "st", fake_st)

    assert not route_memory.restore_internal_route_after_login(current_route_requested=True)
    assert fake_st.query_params == {}
    assert fake_st.session_state[route_memory.INTERNAL_ROUTE_SESSION_KEY] == "qbo"


def test_restore_internal_route_ignores_unknown_memory(monkeypatch):
    fake_st = _fake_st(logged_in=True)
    fake_st.session_state[route_memory.INTERNAL_ROUTE_SESSION_KEY] = "unknown"
    monkeypatch.setattr(route_memory, "st", fake_st)

    assert not route_memory.restore_internal_route_after_login(current_route_requested=False)
    assert fake_st.query_params == {}
    assert route_memory.INTERNAL_ROUTE_SESSION_KEY not in fake_st.session_state
