"""Multiprocess path (T-022): jobs=2 parity + OCR event merge round-trip."""
from fermi_organizer import extraction
from fermi_organizer.runmodes import run_full


def _write_tree(make_pdf, folder):
    make_pdf(folder / "F10126106.pdf", [
        "FERMI PART LIST",
        "F10126107 CHILD PART",
        "NAME",
        "Parent",
    ])
    make_pdf(folder / "F10126107.pdf", [
        "NAME",
        "Child part",
        "USED ON",
        "F10126106",
    ])
    make_pdf(folder / "F10126108.pdf", ["NAME", "Standalone part"])


def test_run_full_jobs2_matches_jobs1(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_tree(make_pdf, in_dir)

    extraction.OCR.reset(enabled=False)
    ctx1 = run_full(in_dir, tmp_path / "out1", True, lambda msg: None, jobs=1)
    extraction.OCR.reset(enabled=False)
    ctx2 = run_full(in_dir, tmp_path / "out2", True, lambda msg: None, jobs=2)

    assert set(ctx1) == set(ctx2)
    assert ctx1["counters"] == ctx2["counters"]
    assert ctx1["counters"]["copies"] == ctx2["counters"]["copies"] == 3
    assert ctx1["missing"] == ctx2["missing"] == []
    assert ctx1["orphans"] == ctx2["orphans"]
    assert ctx1["roots"] == ctx2["roots"]
    assert ctx1["used_on_bugs"] == ctx2["used_on_bugs"]
    assert ctx1["used_on_mismatches"] == ctx2["used_on_mismatches"]
    assert ctx1["names"] == ctx2["names"]
    assert ctx1 == ctx2


def test_run_parallel_uses_spawn_context(monkeypatch):
    import concurrent.futures as cf

    captured = {}

    class FakeExecutor:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, task, paths):
            return [task(p) for p in paths]

    monkeypatch.setattr(cf, "ProcessPoolExecutor", FakeExecutor)
    out = extraction.run_parallel(lambda p: (p.upper(), None), ["a", "b"], jobs=2)
    assert out == [("A",), ("B",)]  # trailing OCR delta stripped
    assert captured["mp_context"].get_start_method() == "spawn"


def test_ocr_event_delta_merge_roundtrip():
    ocr = extraction._OcrContext()
    ocr.events["a.pdf"] = {"pages": {0}, "used_on": ["F10126106"],
                           "name": "Part", "bom": ["F10126107"], "secs": 1.0}
    delta = ocr.event_delta(set())
    assert delta["a.pdf"] == {"pages": [0], "used_on": ["F10126106"],
                              "name": "Part", "bom": ["F10126107"], "secs": 1.0}
    assert ocr.event_delta(set(delta)) == {}

    merged = extraction._OcrContext()
    merged.merge_events(delta)
    assert merged.events["a.pdf"]["pages"] == {0}
    assert merged.events["a.pdf"]["used_on"] == ["F10126106"]
    assert merged.events["a.pdf"]["name"] == "Part"
    assert merged.events["a.pdf"]["bom"] == ["F10126107"]
    assert merged.event_delta(set()) == delta
    merged.merge_events(None)
    assert merged.event_delta(set()) == delta
