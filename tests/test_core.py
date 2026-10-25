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


def test_pin_moves_onto_the_viaduct_next_door():
    score = np.zeros((40, 40))
    score[20, 20] = 1.0   # the best cell is the field...
    score[20, 25] = 0.96  # ...250 m from the viaduct, which is nearly as good
    viaduct = np.zeros_like(score, bool)
    viaduct[20, 25] = True
    everywhere = np.ones_like(score, bool)
    assert scoring.pick_spots(score, everywhere, spacing_m=400, n=1, anchor=viaduct) == [(20, 25)]
    score[20, 25] = 0.5   # much worse on the viaduct itself: the pin stays in the field
    assert scoring.pick_spots(score, everywhere, spacing_m=400, n=1, anchor=viaduct) == [(20, 20)]


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
    cells2["elev"][:, 50:] = 200.0  # same ridge in the east doesn't matter for sunset, only sunrise
    view = derived.sunset_view(cells2)
    assert view["sunset_horizon"][30, 40] < 1 and view["sunrise_horizon"][30, 40] > 10


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
                            "bando_m", "beach_m", "sea_m", "landmark_m", "west_edge_m")}
    stats.update(lone_h_max=10.0, open_r_m=100, max_height_m=12.0, relief_m=20.0, sunset_deg=1.0, sunrise_deg=1.0,
                 bld_h_max=0.0)
    sim = np.full((3, 3), 0.5)
    res = {"top": 0.4, "sim": sim}
    out = export.why(comp, p, stats, res, 1, 1)
    rule = scoring.rule_score({k: np.array(v) for k, v in comp.items()}, p)
    expected = 100 * ((1 - scoring.TASTE) * float(rule) / 0.4 + scoring.TASTE * 0.5)
    assert abs(sum(d["pts"] for d in out) - expected) < 1.5
    assert out[0]["pts"] >= out[-1]["pts"] and all(d["text"] for d in out)


def test_regions_config_is_sane():
    from spotfinder.config import load_regions

    regions = load_regions()
    assert "newcastle" in regions
    for r in regions.values():
        xmin, ymin, xmax, ymax = r.bbox
        assert xmin < xmax and ymin < ymax
        assert all(v % 1000 == 0 for v in r.bbox), f"{r.key}: keep boxes on whole km so LiDAR tiles line up"
        assert r.osm, f"{r.key}: list the Geofabrik extracts covering it"


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


def test_only_real_derelict_buildings_are_bandos():
    # derelict, abandoned or disused buildings and works are
    assert bandos.is_bando({"building": "ruins"}) and bandos.is_bando({"building": "yes", "abandoned": "yes"})
    assert bandos.is_bando({"building": "industrial", "disused:man_made": "pumping_station"})
    assert bandos.is_bando({"building": "warehouse", "disused:military": "ammunition"})
    assert bandos.is_bando({"building": "yes", "building:condition": "ruinous"})
    # old land, pits and railways aren't, nor is a lighthouse that's a house now, nor one that's gone
    assert not bandos.is_bando({"building": "ruins", "demolished:building": "yes"})
    assert not bandos.is_bando({"abandoned:landuse": "quarry"})
    assert not bandos.is_bando({"historic": "mine"}) and not bandos.is_bando({"historic": "mine_shaft"})
    assert not bandos.is_bando({"abandoned:railway": "rail"})
    assert not bandos.is_bando({"abandoned:man_made": "spoil_heap"})
    assert not bandos.is_bando({"building": "residential", "disused:man_made": "lighthouse"})
    # monuments with keepers are left out; a listed building left to rot isn't
    assert bandos.heritage({"historic": "ruins", "designation": "scheduled_monument"})
    assert bandos.heritage({"historic": "ruins", "name": "Cliffords Fort"})
    assert bandos.heritage({"building": "ruins", "ruins": "yes", "name": "St Catherine's Chapel"})
    assert not bandos.heritage({"building": "abandoned", "name": "Ebenezer Methodist Church"})
    assert not bandos.heritage({"historic": "ruins", "ruins": "lime_kiln", "name": "Southwick Limekilns"})
    assert not bandos.heritage({"building": "yes", "disused": "yes", "listed_status": "Grade II"})


def test_viaducts_and_famous_bridges_by_their_own_names():
    from spotfinder.sources import osm

    plessey = {"bridge": "yes", "railway": "rail", "name": "East Coast Main Line", "bridge:name": "Plessey Viaduct",
               "wikidata": "Q26647008"}
    causey = {"bridge": "yes", "highway": "footway", "name": "Causey Arch", "listed_status": "Grade I"}
    line_only = {"bridge": "yes", "railway": "rail", "name": "Tanfield Railway", "wikidata": "Q7682581"}
    assert osm.is_viaduct(plessey) and osm.is_viaduct({"bridge": "viaduct"})
    assert not osm.is_viaduct(causey) and not osm.is_viaduct({"name": "Viaduct Road", "highway": "primary"})
    assert osm.famous_bridge(causey) and osm.famous_bridge(plessey)
    assert not osm.famous_bridge(line_only)  # the railway's Wikidata entry, not the bridge's
    assert not osm.famous_bridge({"bridge": "yes", "highway": "footway", "bridge:name": "Borough Road Footbridge",
                                  "wikidata": "Q118218739"})
    assert not osm.famous_bridge({"listed_status": "Grade II", "building": "yes"})  # not a bridge



def test_spot_names_say_where_not_what():
    from spotfinder.export import display_name, proper

    assert not any(proper({"name": n}) for n in ("Bandstand", "The Green", "Over 8 Play Area", "colliery", "Metro",
                                                  "5 A Side pitch", "Boating Lake"))
    assert all(proper({"name": n}) for n in ("West Park", "North Beach", "Clara Vale Memorial Garden", "Town Moor"))
    # OSM maps stretches of the Wear as just "Wear"
    assert display_name({"name": "Wear", "water": "river"}) == "River Wear"
    assert display_name({"name": "Ouseburn", "waterway": "river"}) == "Ouseburn"
    assert display_name({"name": "River Tyne", "waterway": "river"}) == "River Tyne"
    assert display_name({"name": "former Calders site", "landuse": "brownfield"}) == "Former Calders site"


def test_where_is_a_town_not_a_business_park():
    from spotfinder.export import is_place, place_cost

    # Penshaw Monument: Cox Green is a bit nearer, but it's in Penshaw
    assert place_cost({"place": "village"}, 1247) < place_cost({"place": "hamlet"}, 1175)
    assert not is_place({"name": "Swans Offshore Energy Park", "place": "locality"})
    assert not is_place({"name": "Middle Docks (Disused)", "place": "locality"})
    assert is_place({"name": "Lizard Point", "place": "locality"})
    assert is_place({"name": "Spring Gardens", "place": "suburb"})


def test_heroes_stand_up():
    from spotfinder.context import stands_up

    angel = {"name": "Angel of the North", "tourism": "artwork", "wikidata": "Q1325290"}
    assert stands_up(angel, up_m=19.8, area_m2=66)
    assert not stands_up(angel, up_m=0.0, area_m2=66)  # without LiDAR it's only a sculpture
    assert stands_up({"name": "Prudhoe Castle", "historic": "castle"}, area_m2=1004)
    assert stands_up({"name": "Northumberlandia", "historic": "geoglyph"}, up_m=0.0, area_m2=54184)  # a hill
    assert not stands_up({"name": "Disappearing Gun Turret", "historic": "monument"}, up_m=3.2, area_m2=136)
    assert stands_up({"name": "Column to Liberty", "historic": "monument"})  # a point: its name says so
    assert not stands_up({"name": "Charlton Memorial Fountain", "historic": "monument"})

def test_a_wartime_pillbox_is_not_mod_land():
    import geopandas as gpd
    from shapely.geometry import box

    from spotfinder import legal

    areas = gpd.GeoDataFrame({"tags": [
        {"building": "yes", "military": "bunker"},  # on St Mary's Island
        {"military": "bunker", "name": "Dawdon Blast Beach machine gun emplacement"},
        {"landuse": "military", "name": "Army Reserve Centre"},
        {"building": "yes", "military": "barracks"},
        {"landuse": "military", "military": "bunker"},  # a bunker on a base is still the base
    ]}, geometry=[box(0, 0, 10, 10)] * 5, crs="EPSG:27700")
    assert list(legal.classify_areas(areas)["military"]) == [False, False, True, True, True]


def test_heritage_owners_need_permission():
    import geopandas as gpd
    from shapely.geometry import box

    areas = gpd.GeoDataFrame({"tags": [
        {"historic": "castle", "name": "Prudhoe Castle", "operator": "English Heritage"},
        {"historic": "monument", "name": "West Backworth Medieval Settlement (English Heritage Scheduled Monument)"},
        {"historic": "monument", "name": "Penshaw Monument", "wikidata": "Q7164734"},  # NT, not tagged in OSM
        {"historic": "monument", "name": "Grey's Monument"},
        {"landuse": "forest", "name": "Chopwell Wood"},  # Forestry England, by its OSM id
    ], "osm_id": ["w1", "n2", "w3", "w4", "r11351599"]}, geometry=[box(0, 0, 10, 10)] * 5, crs="EPSG:27700")
    cls = legal.classify_areas(areas)
    assert list(cls["english_heritage"]) == [True, False, False, False, False]
    assert list(cls["national_trust"]) == [False, False, True, False, False]
    assert list(cls["forestry"]) == [False, False, False, False, True]


def test_a_country_park_is_recreation_whatever_its_tag():
    import geopandas as gpd
    from shapely.geometry import box

    from spotfinder.sources.osm import is_country_park

    tags = [
        {"leisure": "nature_reserve", "name": "Bolam Lake Country Park"},
        {"boundary": "protected_area", "name": "Waldridge Fell Country Park"},
        {"amenity": "parking", "name": "Plessey Woods Country Park"},
        {"landuse": "residential", "name": "Low Carrs Country Park"},  # static caravans
        {"leisure": "nature_reserve", "name": "Gosforth Park Nature Reserve"},
    ]
    assert [is_country_park(t) for t in tags] == [True, True, False, False, False]
    areas = gpd.GeoDataFrame({"tags": tags}, geometry=[box(0, 0, 10, 10)] * 5, crs="EPSG:27700")
    assert list(legal.classify_areas(areas)["recreational"]) == [True, True, False, False, False]


def test_permission_by_share_of_the_spot():
    flags = np.zeros((9, 9), "float32")
    flags[4, 4] = legal.FLAG_NATIONAL_TRUST  # one monument cell in the middle
    flags[:, 7:] = legal.FLAG_GOLF | legal.FLAG_RAILWAY  # a golf course down one side, a railway along it
    flags[0, 0] = legal.FLAG_RAILWAY  # only a warning, not someone's permission
    perm = scoring.permission(flags, 2)
    assert 0 < perm[4, 4] < 0.5  # a monument you can take off beside
    assert perm[4, 8] == 1.0  # the middle of the golf course
    assert perm[0, 0] == perm[1, 1] == 0


def test_ruins_in_a_monuments_grounds_are_not_bandos():
    import geopandas as gpd
    from shapely.geometry import box

    from spotfinder import bandos

    areas = gpd.GeoDataFrame({"tags": [
        {"historic": "abbey", "designation": "scheduled_monument", "name": "Finchale Priory"},  # 0.7 ha of grounds
        {"boundary": "administrative", "designation": "civil_parish", "name": "Framwellgate Moor"},
        {"designation": "area_of_outstanding_natural_beauty"},  # a whole landscape
        {"leisure": "park"},
        {"building": "industrial", "abandoned": "yes"},
    ]}, geometry=[box(0, 0, 70, 100), box(0, 0, 4000, 3000), box(0, 0, 2000, 2000), box(0, 0, 50, 50),
                  box(0, 0, 30, 30)], crs="EPSG:27700")
    assert list(bandos.off_limits(areas).index) == [0, 3]


def test_bando_labels_say_what_it_was():
    assert bandos.kind({"building": "ruins"}) == "Ruins"
    assert bandos.kind({"building": "warehouse", "disused:military": "ammunition"}) == "Old military building"
    assert bandos.kind({"building": "industrial", "disused:man_made": "pumping_station"}) == "Old pumping station"
    assert bandos.kind({"building": "yes", "disused": "yes", "disused:amenity": "pub"}) == "Old pub"
    assert bandos.kind({"building": "industrial", "abandoned": "yes"}) == "Old factory"
    assert bandos.kind({"abandoned": "yes", "building": "apartments"}) == "Abandoned building"
    assert bandos._osm_url("w4048915") == "https://www.openstreetmap.org/way/4048915"
    assert bandos._osm_url("x1") is None


def test_lines_keep_off_homes_and_roads():
    from spotfinder import lines

    n = 200
    land = np.zeros((n, n), "uint8")
    land[:, :60] = 1  # houses and gardens down the left
    road = np.zeros((n, n), bool)
    road[150:154, :] = True
    d_road = __import__("scipy").ndimage.distance_transform_edt(~road)
    blocked = lines.keep_out(land, None, d_road, lines.FREESTYLE)
    nodes = lines.nodes_from([(100, 30, 6.0, 10.0, 40.0, True, 1.0), (100, 100, 6.0, 10.0, 40.0, True, 1.0),
                              (100, 140, 6.0, 10.0, 40.0, True, 1.0), (152, 120, 6.0, 10.0, 40.0, True, 1.0)],
                             [], np.zeros((n, n), bool), lines.FREESTYLE, blocked)
    assert sorted(nodes[:, 1].tolist()) == [100, 140]  # the garden gap and the one on the road are gone
    # a link across the road is blocked; one alongside it isn't
    nodes = np.array([[120, 100, lines.GAP, 1.0, 6], [180, 100, lines.GAP, 1.0, 6], [120, 150, lines.GAP, 1.0, 6]])
    nb = lines.links(nodes, np.zeros((n, n), "float32"), lines.FREESTYLE, blocked)
    assert nb[0] == [2]


def test_lines_keep_out_of_school_grounds():
    from spotfinder import lines
    from spotfinder.rasterize import INSTITUTION_CODE
    from spotfinder.sources.osm import is_institution

    land = np.zeros((50, 50), "uint8")
    land[:, 30:] = INSTITUTION_CODE  # Ryton Junior School's playing field
    blocked = lines.keep_out(land, None, None, lines.WHOOP)
    assert blocked[:, 30:].all() and not blocked[:, :30].any()
    assert is_institution({"amenity": "school", "name": "Ryton Junior School"})
    assert is_institution({"amenity": "hospital", "name": "Royal Victoria Infirmary"})
    assert not is_institution({"landuse": "recreation_ground"})


def test_lines_keep_off_working_railways():
    from spotfinder import lines
    from spotfinder.rasterize import RAILWAY_CODE
    from spotfinder.sources.osm import is_active_railway

    land = np.zeros((50, 50), "uint8")
    land[20:37, :] = RAILWAY_CODE  # the East Coast Main Line and 8 m either side
    blocked = lines.keep_out(land, None, None, lines.WHOOP)
    assert blocked[20:37].all() and not blocked[:20].any() and not blocked[37:].any()
    assert is_active_railway({"railway": "rail", "name": "East Coast Main Line"})
    assert is_active_railway({"railway": "light_rail", "name": "Metro"})
    assert not is_active_railway({"railway": "subway", "tunnel": "yes"})  # under the city centre
    assert not is_active_railway({"railway": "disused", "name": "Derwent Valley Railway"})
    assert not is_active_railway({"railway": "abandoned"})


def test_lines_keep_out_of_playgrounds_and_from_under_pylon_lines():
    from spotfinder import lines
    from spotfinder.rasterize import KEEP_OFF, PLAYGROUND_CODE, PYLON_CODE, VIADUCT_CODE

    assert VIADUCT_CODE not in KEEP_OFF  # you fly the arches
    land = np.zeros((40, 40), "uint8")
    land[5:15, 5:15] = PLAYGROUND_CODE
    land[:, 30:] = PYLON_CODE
    blocked = lines.keep_out(land, None, None, lines.FREESTYLE)
    assert blocked[5:15, 5:15].all() and blocked[:, 30:].all()
    assert blocked.sum() == 100 + 400


def test_power_lines_overhead_not_buried():
    from spotfinder.sources import osm

    assert osm.is_power_line({"power": "line"}) and osm.is_power_line({"power": "minor_line"})
    assert not osm.is_power_line({"power": "line", "location": "underground"})
    assert not osm.is_power_line({"power": "cable"}) and not osm.is_power_line({"power": "tower"})


def test_a_bridge_carrying_trains_is_not_one_to_fly_round():
    import geopandas as gpd
    from shapely.geometry import LineString

    from spotfinder.context import off_the_tracks

    tracks = gpd.GeoSeries([LineString([(0, 0), (1000, 0)])])  # the Durham Coast Line
    bridges = gpd.GeoSeries([
        LineString([(400, 0), (600, 0)]),      # Hawthorn Dene Viaduct: the railway runs along it
        LineString([(300, -60), (300, 60)]),   # a road bridge over the line
        LineString([(0, 500), (200, 500)]),    # a footbridge over a river nowhere near it
    ])
    kept = off_the_tracks(bridges, tracks)
    assert list(kept.index) == [1, 2]
    assert len(off_the_tracks(bridges, gpd.GeoSeries([], dtype="geometry"))) == 3


def test_what_the_take_off_stands_on(monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box

    from spotfinder import export
    from spotfinder.sources import osm

    areas = gpd.GeoDataFrame({"tags": [
        {"leisure": "park", "name": "Derwent Walk Country Park"},
        {"landuse": "farmland"},                       # a grazed field inside the park
        {"natural": "beach", "name": "Longsands"},
        {"leisure": "park", "name": "Over 8 Play Area"},
    ]}, geometry=[box(0, 0, 1000, 1000), box(100, 100, 300, 300), box(2000, 0, 2200, 100), box(5000, 0, 5050, 50)])
    monkeypatch.setattr(osm, "extract", lambda region: {"areas": areas})
    g = export.Ground(None)
    assert g.at(500, 500) == ("public", "Derwent Walk Country Park")
    assert g.at(200, 200) == ("farm", None)
    assert g.at(2100, 50) == ("beach", "Longsands")
    assert g.at(5025, 25) == ("public", None)  # a name that only says what it is
    assert g.at(9000, 9000) == (None, None)


def test_viaduct_deck_gives_arches():
    from spotfinder import lines

    n = 120
    deck = np.zeros((n, n), bool)
    deck[58:63, 10:110] = True
    ohm = np.zeros((n, n), "float32")
    ohm[58:63, 10:110] = np.r_[np.linspace(2, 25, 50), np.linspace(25, 2, 50)]  # high over the middle
    a = lines.arches(deck, ohm)
    assert 4 <= len(a) <= 7 and all(h >= lines.ARCH_MIN_H for _, _, h in a)


def test_run_weaves_through_neighbouring_arches():
    from spotfinder import lines

    ohm = np.zeros((200, 200), "float32")
    ohm[98:103, 10:190] = 20  # the deck is solid to LiDAR all the way along
    # seven arches 15 m apart, and one far end of the viaduct beyond a missing arch
    arch = [(100.0, 20.0 + 15 * k, 20.0) for k in range(7)] + [(100.0, 170.0, 20.0)]
    nodes = lines.nodes_from([], [], np.zeros((200, 200), bool), lines.FREESTYLE, arch=arch)
    nb = lines.links(nodes, ohm, lines.FREESTYLE)
    assert sorted(nb[3]) == [2, 4] and nb[7] == []
    score, path = lines.best_run(0, nodes, nb)
    assert path == list(range(lines.MAX_ARCHES))  # capped, so a long viaduct doesn't swamp every other line
    assert abs(score - lines.VALUE[lines.ARCH] * lines.MAX_ARCHES) < 1e-9
    ohm[85:92, 60:80] = 15  # trees crowding one side of the viaduct between the 4th and 5th arches
    nb = lines.links(nodes, ohm, lines.FREESTYLE)
    assert 4 not in nb[3] and sorted(nb[2]) == [1, 3]



def test_takeoff_leg_stays_off_homes():
    from spotfinder import lines

    ohm = np.zeros((200, 200), "float32")
    open_ = np.ones((200, 200), bool)
    blocked = np.zeros((200, 200), bool)
    blocked[60:80, :] = True  # a street of houses between the path (north) and the first gap
    tk = lines.takeoff(np.array([100.0, 100.0]), 6.0, ohm, open_, lines.FREESTYLE, blocked, hint=(40.0, 100.0))
    assert tk is not None and tk[0] > 80  # this side of the houses, not across them
    assert 6.0 + lines.TAKEOFF_MIN <= np.hypot(tk[0] - 100, tk[1] - 100) <= lines.TAKEOFF_MAX
    open_[80:94, :] = False  # a pond between the houses and the gap...
    ohm[107:200, 95:106] = 10  # ...and a wall behind it: the leg has to come in from the side
    tk = lines.takeoff(np.array([100.0, 100.0]), 6.0, ohm, open_, lines.FREESTYLE, blocked, hint=(40.0, 100.0))
    assert tk is not None and tk[0] >= 94 and not (tk[0] >= 105 and 93 <= tk[1] <= 107)


def test_line_links_need_clear_air():
    from spotfinder import lines

    ohm = np.zeros((200, 200), "float32")
    nodes = np.array([[100, 50, lines.LONE, 1.0, 6], [100, 90, lines.GAP, 1.0, 6], [100, 130, lines.LONE, 1.0, 6]])
    nb = lines.links(nodes, ohm, lines.FREESTYLE)
    assert nb[0] == [1] and sorted(nb[1]) == [0, 2]
    ohm[80:120, 110] = 10  # a wall between the gap and the last tree
    nb = lines.links(nodes, ohm, lines.FREESTYLE)
    assert nb[1] == [0] and nb[2] == []


def test_a_link_bends_round_a_clump_of_trees():
    from spotfinder import lines

    ohm = np.zeros((200, 200), "float32")
    nodes = np.array([[100, 50, lines.LONE, 1.0, 6], [100, 100, lines.GAP, 1.0, 6]])
    ohm[95:106, 70:81] = 12  # a clump of trees right in the way
    assert lines.links(nodes, ohm, lines.FREESTYLE) == [[], []]  # no way straight through
    via = {}
    nb = lines.links(nodes, ohm, lines.FREESTYLE, via=via)
    assert nb == [[1], [0]] and (0, 1) in via
    r, c = via[(0, 1)]
    assert abs(c - 75) < 1 and abs(r - 100) >= 0.25 * 50 - 1  # off to one side of the clump
    straight = lines.path_length(nodes, [0, 1])
    assert straight == 50 and straight < lines.path_length(nodes, [0, 1], via) <= 1.3 * straight
    # the walk leaves along the first leg and arrives along the second
    nodes = np.vstack([nodes, [[100, 150, lines.GAP, 1.0, 6]]])
    via = {}
    nb = lines.links(nodes, ohm, lines.FREESTYLE, via=via)
    score, path = lines.best_run(0, nodes, nb, via)
    assert path == [0, 1, 2] and score < sum(lines.VALUE[int(k)] for k in nodes[:, 2]) * 1.15
    ohm[60:141, 70:81] = 12  # a whole wood: no bend gets round it
    via = {}
    assert lines.links(nodes[:2], ohm, lines.FREESTYLE, via=via) == [[], []] and not via


def test_run_prefers_variety_and_flow():
    from spotfinder import lines

    ohm = np.zeros((300, 300), "float32")
    # a straight run east: tree, gap, dive tree, gap; plus a skim node off at a hairpin angle
    nodes = np.array([[150, 30, lines.LONE, 1.0, 6], [150, 70, lines.GAP, 1.0, 6], [150, 110, lines.DIVE, 1.0, 6],
                      [150, 150, lines.GAP, 1.0, 6], [140, 60, lines.SKIM, 1.0, 6]])
    nb = lines.links(nodes, ohm, lines.FREESTYLE)
    score, path = lines.best_run(0, nodes, nb)
    assert path[:4] == [0, 1, 2, 3]
    assert score > sum(lines.VALUE[k] for k in (lines.LONE, lines.GAP, lines.DIVE, lines.GAP))


def test_analyse_reports_line_cells():
    n, m = 1080, 40
    ohm = np.zeros((n, n))
    for c in range(300, 700, 40):  # a row of separate trees across a field: orbit, gap, orbit...
        ohm[500:508, c:c + 8] = 12
    cells, _, _ = features.analyse(make_tile(ohm), margin=m, size=1000)
    assert cells["line5"].max() > 4 and cells["line5_n"].max() >= 5


def test_a_woods_edge_is_skimmed_from_the_field():
    from spotfinder import lines

    n, m = 1080, 40
    ohm = np.zeros((n, n))
    ohm[300:700, 200:500] = 12  # a wood in the middle of a field...
    ohm[490:500, 200:500] = 0  # ...with a 10 m ride through it
    _, _, _, runs = features.analyse(make_tile(ohm), margin=m, size=1000, want_lines=True)
    nodes = runs["line5"][0]
    lane = nodes[(nodes[:, 2] == lines.SKIM) & (nodes[:, 4] == lines.LANE_R)]
    assert len(lane) >= 20  # all round the wood, one every SKIM_STEP or so
    r, c = lane[:, 0], lane[:, 1]
    ride = (r >= 490) & (r < 500)
    off = np.maximum.reduce([300 - r, r - 699, 200 - c, c - 499])[~ride]  # metres out from the wood
    assert (off >= lines.LANE_IN - 1).all() and (off <= lines.LANE_OUT + 1).all() and (lane[:, 3] == 1.0).all()
    assert not (ride & (c >= 210) & (c < 490)).any()  # not along the ride, past its mouths: no room there
    assert not (runs["linew"][0][:, 4] == lines.LANE_R).any()  # the whoop sticks to tree lines
    assert runs["line5"][1].max() > 2  # and they chain into a run along the edge


def test_lines_dont_fly_through_trees_hanging_over_a_river(monkeypatch):
    from spotfinder import lines

    seen = []
    links = lines.links
    monkeypatch.setattr(lines, "links", lambda nodes, ohm, *a, **k: seen.append(ohm) or links(nodes, ohm, *a, **k))
    n, m = 1080, 40
    t = make_tile(np.zeros((n, n)))
    t.water[480:530, :] = True  # a river, with trees hanging over its bank
    t.over_water = np.zeros((n, n), "float32")
    t.over_water[480:490, 300:400] = 9
    features.analyse(t, margin=m, size=1000)
    assert seen and all(o[485, 350] == 9 and o[520, 350] == 0 for o in seen)


def test_tree_in_a_loose_clump_is_not_an_orbit():
    n, m = 1080, 40
    ohm = np.zeros((n, n))
    ohm[600:606, 300:306] = 12
    ohm[600:606, 314:320] = 10  # a second tree 8 m away: the first passes its Voronoi ring, not an orbit
    _, _, lone = features.analyse(make_tile(ohm), margin=m, size=1000)
    assert len(lone) == 0
    ohm[600:606, 314:320] = 2  # just a low bush: still a fine orbit
    _, _, lone = features.analyse(make_tile(ohm), margin=m, size=1000)
    assert len(lone) == 1
