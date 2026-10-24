"""Fast checks on the bits that are easy to get subtly wrong. No network, no big data."""

import numpy as np

from spotfinder import features, legal, scoring
from spotfinder.export import os_grid_ref
from spotfinder.heights import Tile
from spotfinder.sources import lidar_wcs


def make_tile(ohm, trees=None, bld=None):
    n = ohm.shape[0]
    z = np.zeros((n, n), dtype=bool)
    return Tile(x0=0, y1=n, ohm=ohm.astype("float32"), trees=(ohm if trees is None else trees).astype("float32"),
                bldh=np.zeros((n, n), "float32"), bld=z if bld is None else bld, water=z.copy(),
                ways=np.zeros((n, n), "uint8"), porosity=None, lidar=z.copy())


def test_grid_ref_newcastle():
    assert os_grid_ref(424000, 564000) == "NZ240640"
    assert os_grid_ref(424567, 564321, digits=8) == "NZ24566432"


def test_parse_lower_limits():
    assert legal.parse_lower_ft("SFC") == 0
    assert legal.parse_lower_ft("FL 45") == 4500
    assert legal.parse_lower_ft("2,000 FT ALT") == 2000
    assert legal.parse_lower_ft(None) == 0


def test_gap_between_two_tree_blocks_is_found():
    n, m = 1080, 40  # a 1 km tile plus the 40 m margin all round
    ohm = np.zeros((n, n))
    # two 10 m tall stands of trees, 6 m apart, in the middle of the tile
    ohm[500:530, 500:520] = 10
    ohm[500:530, 526:546] = 10
    cells, gaps, lone = features.analyse(make_tile(ohm), margin=m, size=1000)
    widths = [g[2] for g in gaps]
    assert any(4 <= w <= 8 for w in widths), widths
    assert all(g[3] >= features.GAP_MIN_FLANK for g in gaps)


def test_lone_tree_in_a_field():
    n, m = 1080, 40
    ohm = np.zeros((n, n))
    ohm[600:606, 300:306] = 12  # one 12 m tree, nothing else around
    cells, gaps, lone = features.analyse(make_tile(ohm), margin=m, size=1000)
    assert len(lone) == 1
    assert lone[0][2] == 12
    # and a tree inside a wood is not lone
    ohm[580:640, 280:330] = 12
    _, _, lone2 = features.analyse(make_tile(ohm), margin=m, size=1000)
    assert len(lone2) == 0


def test_window_or_keeps_every_bit():
    flags = np.zeros((5, 5), "int64")
    flags[1, 1] = legal.FLAG_FORESTRY
    flags[1, 2] = legal.FLAG_CROWDS
    out = scoring.win_or(flags, 1)
    assert out[2, 2] & legal.FLAG_FORESTRY and out[2, 2] & legal.FLAG_CROWDS
    assert out[4, 4] == 0


def test_pick_spots_respects_spacing():
    rng = np.random.default_rng(1)
    score = rng.random((60, 60))
    picked = scoring.pick_spots(score, np.ones_like(score, bool), spacing_m=400, n=30)
    pts = np.array(picked) * scoring.CELL
    d = np.sqrt(((pts[:, None] - pts[None]) ** 2).sum(-1))
    np.fill_diagonal(d, 1e9)
    assert d.min() > 400 - scoring.CELL
    assert picked[0] == tuple(np.unravel_index(score.argmax(), score.shape))


def test_similarity_peaks_at_the_seed():
    rng = np.random.default_rng(2)
    p = scoring.PROFILES["cine85"]
    comps = {k: rng.random((40, 40)) for k in p.style_features}
    sim = scoring.similarity(comps, p, [(10, 10)], np.ones((40, 40), bool))
    assert np.unravel_index(sim.argmax(), sim.shape) == (10, 10)


def test_lidar_encoding_matches_the_shared_pack():
    first = np.full((1000, 1000), 110.0, "float32")
    last = np.full((1000, 1000), 104.0, "float32")
    dtm = np.full((1000, 1000), 100.0, "float32")
    first[0, 0] = np.nan
    out = lidar_wcs.encode(first, last, dtm)
    assert out["ndsm"][1, 1] == 40  # 10 m * 4
    assert out["porosity"][1, 1] == 24  # 6 m * 4
    assert out["ndsm"][0, 0] == lidar_wcs.NODATA_U8
    assert out["dtm2"].shape == (500, 500) and out["dtm2"][3, 3] == 100
    tall = lidar_wcs.encode(first + 100, last, dtm)
    assert tall["ndsm"][5, 5] == 254  # clipped at 63.5 m, 255 stays no-data
