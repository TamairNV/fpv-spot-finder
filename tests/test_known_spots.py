from spotfinder import evaluate, scoring
from spotfinder.config import load_region


def test_known_spots_are_sane():
    spots = evaluate.load_known()
    assert len(spots) >= 20
    xmin, ymin, xmax, ymax = load_region("newcastle").bbox
    for s in spots:
        assert s.profiles and set(s.profiles) <= set(scoring.PROFILES), s.name
        assert s.why and s.kinds, s.name
        x, y = s.bng
        assert xmin <= x <= xmax and ymin <= y <= ymax, f"{s.name} is outside the Newcastle box"


def test_report_counts_statuses():
    rows = [dict(spot="a", profile="cine5", kinds=["beach"], status="found", rank=3, pct=99.0, score=80,
                 eligible=True, sinks=[]),
            dict(spot="b", profile="cine5", kinds=["beach"], status="missed", rank=None, pct=50.0, score=20,
                 eligible=False, sinks=[("hazard", -0.25)])]
    text = evaluate.report(rows)
    assert "1 found" in text and "1 missed" in text and "beach 1/2" in text and "hazard -0.25" in text


def test_bando_checks_are_sane():
    yes, no = evaluate.load_bando_checks()
    assert yes and no
    for b in yes + no:
        assert {"name", "lat", "lon", "why"} <= set(b), b
