"""Fuel-change alert: silent (TaskNoop) on seed + unchanged, emits on change.

Mocks the carbu.com scrape and points DATA_DIR at a tmp dir so it's offline
and hermetic.
"""
import asyncio
import json
import os

import pytest

import src.builtin_actions as ba
from src.builtin_actions import action_fuel_price_alert, TaskNoop


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    prices = {"Diesel (B7)": ("2,0800", "2,0800", "="),
              "Super 95 (E10)": ("1,8740", "1,8740", "="),
              "Super 98 (E5)": ("1,9530", "1,9530", "=")}
    monkeypatch.setattr(ba, "_fetch_fuel_table", lambda wanted: dict(prices))
    return tmp_path, prices


def test_first_run_seeds_silently(offline):
    with pytest.raises(TaskNoop, match="seeded"):
        asyncio.run(action_fuel_price_alert("alice"))
    # state file written
    assert os.path.exists(offline[0] / "fuel_state_alice.json")


def test_unchanged_is_silent(offline):
    asyncio.run(_seed("alice"))
    with pytest.raises(TaskNoop, match="no fuel price change"):
        asyncio.run(action_fuel_price_alert("alice"))


def test_change_emits_message(offline, monkeypatch):
    asyncio.run(_seed("alice"))
    # next scrape returns a higher diesel price
    changed = {"Diesel (B7)": ("2,1200", "2,1200", "="),
               "Super 95 (E10)": ("1,8740", "1,8740", "="),
               "Super 98 (E5)": ("1,9530", "1,9530", "=")}
    monkeypatch.setattr(ba, "_fetch_fuel_table", lambda wanted: dict(changed))
    msg, ok = asyncio.run(action_fuel_price_alert("alice"))
    assert ok
    assert "Diesel" in msg and "2,0800 → 2,1200 ↑" in msg


async def _seed(owner):
    try:
        await action_fuel_price_alert(owner)
    except TaskNoop:
        pass
