"""Fast checks on the bits that are easy to get subtly wrong. No network, no big data."""

import numpy as np

from spotfinder import bandos, features, legal, parks, scoring
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


def test_two_seeds_are_matched_separately():
    p = scoring.PROFILES["cine85"]
    comps = {k: np.zeros((30, 30)) for k in p.style_features}
    comps["water"][5, 5] = 1.0  # one seed by the river
    comps["architecture"][20, 20] = 1.0  # one among buildings
    sim = scoring.similarity(comps, p, [[(5, 5)], [(20, 20)]], np.ones((30, 30), bool))
    assert sim[5, 5] == sim.max() and sim[20, 20] == sim.max()


def test_gap_counts_only_when_you_can_fly_through_it():
    n, m = 1080, 40
    ohm = np.zeros((n, n))
    ohm[500:530, 500:520] = 10
    ohm[500:530, 526:546] = 10  # a gap between two stands, open field both sides
    cells, gaps, _ = features.analyse(make_tile(ohm), margin=m, size=1000)
    assert cells["gaps"].sum() >= 1 and any(g[5] for g in gaps)
    # a clearing in the middle of a wood is a hole, not a line
    wood = np.full((n, n), 12.0)
    wood[500:506, 500:506] = 0
    cells2, gaps2, _ = features.analyse(make_tile(wood), margin=m, size=1000)
    assert cells2["gaps"].sum() == 0 and not any(g[5] for g in gaps2)


def test_tree_line_vs_wood_block():
    n, m = 1080, 40
    ohm = np.zeros((n, n))
    ohm[300:308, 200:400] = 10  # 200 m x 8 m tree line
    ohm[600:700, 600:700] = 10  # 100 m square wood
    cells, _, _ = features.analyse(make_tile(ohm), margin=m, size=1000)
    line = cells["treeline_m"][(300 - m) // 50, (300 - m) // 50]
    wood = cells["treeline_m"][(650 - m) // 50, (600 - m) // 50]
    assert line > 20 and wood == 0
    # west-facing edges: the wood's west side, not its east side
    assert cells["edge_w_m"][(650 - m) // 50, (600 - m) // 50] > 0
    assert cells["edge_w_m"][(650 - m) // 50, (699 - m) // 50] == 0


def _flat_cells(n=60):
    z = lambda v=0.0: np.full((n, n), v, dtype="float32")
    return {"elev": z(), "tall_frac": z(), "h_p90": z(), "open_frac": z(1.0), "water_frac": z(), "road_frac": z(),
            "bld_frac": z()}


def test_sunset_view_sees_a_hill_to_the_west_not_the_east():
    from spotfinder import derived

    cells = _flat_cells()
    cells["elev"][:, :10] = 200.0  # a big ridge along the west side
    hz = derived.sunset_view(cells)["sunset_horizon"]
    assert hz[30, 20] > 10          # just east of the ridge: the sunset is behind the hill
    assert hz[30, 59] < hz[30, 20]  # further away it sits lower
    cells2 = _flat_cells()
    cells2["elev"][:, 50:] = 200.0  # same ridge in the east doesn't matter for sunset
    assert derived.sunset_view(cells2)["sunset_horizon"][30, 40] < 1


def test_open_room_grows_away_from_the_wood():
    from spotfinder import derived

    cells = _flat_cells(20)
    cells["open_frac"][:, 0] = 0.0  # a wood along the west edge
    room = derived.field_size(cells)["open_r_m"]
    assert room[10, 0] == 0 and room[10, 1] == 50 and room[10, 10] == 500


def test_why_points_add_up_to_the_score():
    from spotfinder import export

    p = scoring.PROFILES["freestyle5"]
    comp = {k: 0.5 for k in p.weights}
    stats = {k: 1 for k in ("gaps", "gaps_whoop", "lone_trees", "edge_m", "treeline_m", "open_pct", "tree_pct",
                            "bld_pct", "water_m", "bridge_m", "places", "hazard_m", "access_m", "road_busy_m",
                            "bando_m", "bando_site_m", "landmark_m", "west_edge_m")}
    stats.update(lone_h_max=10.0, open_r_m=100, max_height_m=12.0, relief_m=20.0, sunset_deg=1.0, bld_h_max=0.0)
    sim = np.full((3, 3), 0.5)
    res = {"top": 0.4, "sim": sim}
    out = export.why(comp, p, stats, res, 1, 1)
    rule = scoring.rule_score({k: np.array(v) for k, v in comp.items()}, p)
    expected = 100 * (0.6 * float(rule) / 0.4 + 0.4 * 0.5)
    assert abs(sum(d["pts"] for d in out) - expected) < 1.5
    assert out[0]["pts"] >= out[-1]["pts"] and all(d["text"] for d in out)


def test_every_park_without_a_spot_gets_its_best_cell():
    grid = np.zeros((20, 20), dtype="int32")
    grid[2:6, 2:6] = 1    # park 1 already has a spot in it
    grid[10:16, 10:16] = 2  # park 2 has none
    grid[17:19, 0:2] = 3  # park 3 has nothing eligible
    eligible = np.ones((20, 20), dtype=bool)
    eligible[17:19, 0:2] = False
    score = np.zeros((20, 20))
    score[12, 13] = 0.9
    score[14, 14] = 0.5
    names = [("A", None), ("B", None), ("C", None)]
    assert parks.picks(grid, names, score, eligible, [(3, 3)], spacing_m=400) == [(12, 13)]
    # the best cell is too close to a spot just outside the park, so the next best in the park is used
    assert parks.picks(grid, names, score, eligible, [(3, 3), (9, 12)], spacing_m=400) == [(14, 14)]
    assert parks.name_at(grid, names, 12, 13) == "B" and parks.name_at(grid, names, 0, 0) is None


def test_bando_labels_say_what_it_was():
    assert bandos.kind({"historic": "mine"}) == "Old pit"
    assert bandos.kind({"building": "ruins"}) == "Ruins"
    assert bandos.kind({"building": "warehouse", "disused:military": "yes"}) == "Old military building"
    assert bandos.kind({"historic": "ruins", "ruins": "church"}) == "Ruined church"
    assert bandos.kind({"abandoned:landuse": "quarry"}) == "Old quarry"
    assert bandos.kind({"disused": "yes", "man_made": "adit"}) == "Old adit"
    assert bandos.kind({"building": "yes", "abandoned": "yes"}) == "Abandoned building"
    assert bandos.kind({"military": "bunker", "historic": "ruins"}) == "Bunker"
    assert bandos._osm_url("w4048915") == "https://www.openstreetmap.org/way/4048915"
    assert bandos._osm_url("x1") is None
