"""Shared pytest fixtures. See Fleet HQ's docs/CODING-STANDARDS.md §Testing."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from caobunga import pins as pins_mod
from caobunga import usage as usage_mod
from caobunga.usage import ProviderUsage, RunFn, WhichFn
from main import app

_REAL_PROBE_ALL = usage_mod.probe_all
_REAL_PINS_CHECK_ALL = pins_mod.check_all


@pytest.fixture(autouse=True)
def _block_live_probe_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test reaches live ``usage.probe_all()`` without injection.

    ``_run()`` injects ``usage=`` by convention; this makes hermeticity
    structural. Passthrough when ``which=`` or ``run=`` is supplied so
    ``test_usage`` can still exercise the real library. Patch both the
    usage module and picker's bound import (``from … import probe_all``).
    """

    def guarded_probe_all(
        *,
        which: WhichFn | None = None,
        run: RunFn | None = None,
        timeout: float = usage_mod.DEFAULT_TIMEOUT,
    ) -> list[ProviderUsage]:
        if which is None and run is None:
            msg = (
                "test reached live usage.probe_all(); "
                "inject usage= on pick/execute_run, or which=/run= on probe_all"
            )
            raise RuntimeError(msg)
        return _REAL_PROBE_ALL(which=which, run=run, timeout=timeout)

    monkeypatch.setattr(usage_mod, "probe_all", guarded_probe_all)
    monkeypatch.setattr("caobunga.picker.probe_all", guarded_probe_all)


@pytest.fixture(autouse=True)
def _stub_live_pins_check_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `status <project>` / `digest` tests off the live model catalogs (CBN-256).

    Uninjected ``pins.check_all()`` would shell out to ``grok models`` /
    ``cursor-agent models`` (network, 10s+) on any host that has them. It
    returns no reports instead; passthrough when ``which=`` or ``run=`` is
    supplied, mirroring ``_block_live_probe_all``. (``test_pins`` imports
    ``check_all`` by name, so it holds the real function regardless.)
    """

    def stubbed_check_all(
        *,
        which: pins_mod.WhichFn | None = None,
        run: pins_mod.RunFn | None = None,
        timeout: float = pins_mod.CATALOG_TIMEOUT,
    ) -> list[pins_mod.PinReport]:
        if which is None and run is None:
            return []
        return _REAL_PINS_CHECK_ALL(which=which, run=run, timeout=timeout)

    monkeypatch.setattr(pins_mod, "check_all", stubbed_check_all)


@pytest.fixture
def client() -> TestClient:
    """A TestClient bound to the FastAPI app — use it to exercise routes."""
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def caobunga_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the on-disk store from the real ~/.caobunga/."""
    home = tmp_path / "caobunga-home"
    home.mkdir()
    monkeypatch.setenv("CAOBUNGA_HOME", str(home))
    return home
