import json
import time
from pathlib import Path

from mail_router.lookup import LookupTable


def _write(p: Path, data: dict) -> None:
    p.write_text(json.dumps(data))


def test_resolve_known_ds(tmp_path: Path):
    f = tmp_path / "lookup.json"
    _write(f, {
        "hosts": ["shipard.email"],
        "data_sources": {
            "firma-xyz": {"api_url": "https://shpd.example.com/", "api_token": "tok"}
        },
    })
    t = LookupTable(f)
    ds = t.resolve("firma-xyz")
    assert ds is not None
    assert ds.api_url == "https://shpd.example.com"  # trailing / stripped
    assert ds.api_token == "tok"


def test_resolve_unknown_ds(tmp_path: Path):
    f = tmp_path / "lookup.json"
    _write(f, {"hosts": [], "data_sources": {}})
    t = LookupTable(f)
    assert t.resolve("nope") is None


def test_allowed_domains_lowercased(tmp_path: Path):
    f = tmp_path / "lookup.json"
    _write(f, {"hosts": ["SHIPARD.email"], "data_sources": {}})
    t = LookupTable(f)
    assert "shipard.email" in t.allowed_domains


def test_auto_reload_on_mtime_change(tmp_path: Path):
    f = tmp_path / "lookup.json"
    _write(f, {"hosts": ["a.com"], "data_sources": {}})
    t = LookupTable(f)
    assert "a.com" in t.allowed_domains
    time.sleep(1.01)  # ensure mtime ticks (some FS have 1s resolution)
    _write(f, {"hosts": ["b.com"], "data_sources": {}})
    assert "b.com" in t.allowed_domains
    assert "a.com" not in t.allowed_domains


def test_ds_id_lookup_case_insensitive(tmp_path: Path):
    f = tmp_path / "lookup.json"
    _write(f, {
        "hosts": ["shipard.email"],
        "data_sources": {"FIRMA-XYZ": {"api_url": "https://h", "api_token": "t"}},
    })
    t = LookupTable(f)
    assert t.resolve("firma-xyz") is not None
    assert t.resolve("Firma-Xyz") is not None


def test_missing_file_returns_empty(tmp_path: Path):
    f = tmp_path / "does-not-exist.json"
    t = LookupTable(f)
    assert t.allowed_domains == set()
    assert t.resolve("anything") is None
