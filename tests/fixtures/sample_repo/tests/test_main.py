"""Tests for main — only covers add(), NOT get_user or reports."""

from app.main import add


def test_add():
    assert add(2, 3) == 5
