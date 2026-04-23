from datetime import date, datetime
import pytest
from app_sections.remaining_pages import _coerce_date

def test_coerce_date_with_date_object():
    d = date(2023, 10, 27)
    default = date(2000, 1, 1)
    assert _coerce_date(d, default) is d

def test_coerce_date_with_datetime_object():
    # Now it should return a pure date object, not a datetime object.
    dt = datetime(2023, 10, 27, 12, 0, 0)
    default = date(2000, 1, 1)
    result = _coerce_date(dt, default)
    assert result == dt.date()
    assert type(result) is date
    assert not isinstance(result, datetime)

def test_coerce_date_with_valid_iso_string():
    s = "2023-10-27"
    default = date(2000, 1, 1)
    assert _coerce_date(s, default) == date(2023, 10, 27)

def test_coerce_date_with_valid_iso_datetime_string():
    s = "2023-10-27T12:00:00"
    default = date(2000, 1, 1)
    assert _coerce_date(s, default) == date(2023, 10, 27)

def test_coerce_date_with_empty_string():
    default = date(2000, 1, 1)
    assert _coerce_date("", default) == default

def test_coerce_date_with_whitespace_string():
    default = date(2000, 1, 1)
    assert _coerce_date("   ", default) == default

def test_coerce_date_with_invalid_string():
    default = date(2000, 1, 1)
    assert _coerce_date("not-a-date", default) == default

def test_coerce_date_with_none():
    default = date(2000, 1, 1)
    assert _coerce_date(None, default) == default

def test_coerce_date_with_other_type():
    default = date(2000, 1, 1)
    assert _coerce_date(123, default) == default
