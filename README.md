# FPV Spot Finder

Finds places worth flying an FPV quad in the UK, and tells you straight away how each one sits with the rules.

**Open it:** https://tamairnv.github.io/fpv-spot-finder/ (Newcastle upon Tyne for now).

It doesn't look at satellite photos. Every 50 m square of a region gets measured from 1 m height data
(trees, buildings, gaps between them) and map data (land use, water, bridges, bandos, airspace),
then scored on the things pilots actually look for. Spots you already like steer the ranking, and so
do your 👍 / 👎 in the app.

Three profiles out of the box:

| Profile | Drone | UK category | What it hunts for |
|---|---|---|---|
| `freestyle5` | 5", 800 g with battery and action cam, no class mark | Open **A3** | gaps, lone trees, tree lines, open ground, drops, bandos; each spot says whether it's A3-clear |
| `cine5` | the same 5", flown smooth and cinematic | Open **A3** | rivers and coast, viaducts, hills and banks, landmarks and ruins, big fields, a clear view of the sunset; each spot says whether it's A3-clear |
| `cine85` | 85 mm cinewhoop, under 250 g | Open **A1** | architecture, whoop-sized gaps, plazas, trees, water, bridges, landmarks |

![Spot card with the 3D model](docs/spot-card.png)

## What's in the app

- **Map** with a heatmap of the best 12% of the ground, numbered pins for the top spots, airport/prison
  flight restriction zones, and (zoomed in, greyed out) where the 5" isn't A3-clear.
- **List and spot card in one panel** beside the map on a computer, and in a bottom sheet you drag
  up and down on a phone. Every spot has a badge: A3 clear, partly A3, or sub-250 g only.
- **Spot cards**: why it scored (in words and as a breakdown), what's there (gaps, lone trees,
  edges, tallest thing, drop, distance to water/bando/bridge), a suggested take-off point (off
  gardens, car parks, playgrounds, railways and roads) and what it's on (public open space and its
  name, with a nudge to check the park's byelaws; a beach; or farmland: ask the farmer), the legal
  picture for that drone, land-owner warnings,
  sunrise/golden hour, and an 18-hour wind and rain strip from Open-Meteo.
- **Shots for the 5" cine**: a spot named after a lighthouse, castle, monument or ruin, or by a sea
  stack (Marsden Rock), gets an orbit round it, a bridge or viaduct a pass along its side at deck
  height, and a spot on the cliffs a pass along them level with the top, drawn on the map and in 3D,
  with where the golden-hour sun is (lit from the front, or a silhouette). "Watch the shot" flies it
  with the camera on the subject.
- **3D model of each top spot** (320 m square at 2 m) with the real sun for today: drag the slider
  through the day to see where the shadows and golden light fall. Pink rings are the detected gaps,
  yellow loops the lone trees, green the take-off.
- **Fly it**: a tiny sim inside the 3D model. Keyboard flies in angle mode with climb-rate hold;
  plug your radio in as a USB joystick (EdgeTX/OpenTX "USB Joystick") and it flies acro on the sticks,
  with 5" or whoop-ish rates. Fly through the gap rings to tick them off.
- **Can I fly here?**: press the button and tap the map, or right-click / long-press anywhere, for a
  verdict for both drones, with reasons.
- **Ratings**: 👍 / 👎 any spot. "My taste" re-ranks the list with a small model trained on your
  ratings; "More like this" finds spots similar to one you like. Ratings stay in your browser and can
  be exported and fed back into the pipeline.
- **Filters and notes**: chips above the list narrow it to parks, bandos, water, bridges, hills,
  sunset spots and so on, or to your 👍 spots. **Filters** narrows by the numbers behind each spot:
  a bando within 100 m / 300 m / 1 km, plenty of gaps or lone trees, something tall to dive (steps
  that follow each drone's own spread), water or parking within a distance, and quiet places only;
  shut, the chip says which are on. ‹ › on a spot (or ← →) steps through the list. Each spot takes your own notes (parking, access, when it's quiet); they show as 📝 in the list, are
  searchable, and go in the ratings download.
- **Bandos**: derelict, abandoned and disused buildings and works, as purple dots. The Bandos chip
  lists them, biggest first. Tap one for what it was, how tall it still stands, the nearest spot,
  directions and Street View, and a 3D close-up from the LiDAR at 1 m. Search finds them by name or
  kind ("pub", "factory"; "bandos" lists the lot), and each spot card lists the bandos within 1 km.
  With a mouse, hovering any spot or bando dot names it.
- **Works offline** once loaded (add it to your home screen). When the online map can't load, it
  switches to a built-in basemap (main roads, rail, water, woods, built-up areas, place names), so it
  still works in a field with no signal. **GPX export** of the spots for OS Maps or a GPS, and a share
  link for each spot.
- Links out to Geograph (people's ground-level photos of that 1 km square, often the quickest way
  to judge a place), Street View, satellite and OS Maps.

## Running it

### Just the app (Newcastle data is in the repo)

```bash
cd web
npm install
npm run dev          # http://localhost:5173
```

`npm run build` makes a static site in `web/dist/` you can host anywhere (GitHub Pages, Netlify, a
phone on the same Wi-Fi with `npm run preview -- --host`).

`npm run deploy` builds it and force-pushes `web/dist` to the `gh-pages` branch, which GitHub Pages
serves at https://tamairnv.github.io/fpv-spot-finder/ (it takes a minute or two to go live). The
branch only ever holds the latest build.

### The pipeline (to re-score, change rules, or add a region)

Python 3.12+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

Put two things in `data/static/` first (both free):

- `data/static/osm/`: Geofabrik county extracts covering the region, e.g. `tyne-and-wear`,
  `northumberland` and `durham` from <https://download.geofabrik.de/europe/united-kingdom/england.html>.
- `data/static/nats/`: the NATS UAS restriction zones KMZ (`EG_UAS_FR_DS_AREA1_FULL_YYYYMMDD.kmz`)
  from the NATS AIS digital datasets page. The newest one already in force is used, so drop the next
  AIRAC file in whenever it's published.

Everything else downloads itself and is cached in `data/cache/<region>/`:

- Overture Maps buildings, roads, land use, places (from their public S3 bucket),
- Meta / WRI 1 m global canopy height (public S3) as the fallback height model,
- AWS terrain tiles for ground height.

```bash
python -m spotfinder run --region newcastle      # everything: ~10 min for 40 x 40 km
python -m spotfinder score --region newcastle    # just re-score + re-export (~1 min)
python -m spotfinder legal --region newcastle    # just the legal grids, e.g. after a new AIRAC (~4 min)
pytest                                           # quick checks
```

Outputs land in `web/public/data/<region>/`, which is what the app reads.

### LiDAR (much better heights)

The Environment Agency's national LiDAR is hundreds of GB as downloads, so this doesn't download it.
It asks the EA's Web Coverage Service for just the 1 km squares it needs and keeps a compact version
(about 1.5 MB per km²):

```bash
python -m spotfinder lidar --region newcastle --top 60   # only under the best 60 spots per drone
python -m spotfinder lidar --region newcastle            # the whole region (~1600 squares, ~15 min)
python -m spotfinder features && python -m spotfinder score
```

Squares without LiDAR fall back to the tree-height model, so a partial fetch is fine. Each spot card
says which heights it used.

### Teach it your taste

In the app, 👍 / 👎 re-rank the list straight away ("My taste"). To make the whole region learn,
not just the listed spots: **Download my ratings and notes** (bottom left), save the file as
`config/ratings/<region>.json` and run `python -m spotfinder score`. Thumbs-up spots become extra
seeds; places like your thumbs-down spots get pulled down.

### Check it against real spots

`config/known_spots.toml` lists real places people fly around Newcastle (viaducts, beaches, bandos,
quarries, landmarks), each with the drones it suits and why it's good. `python -m spotfinder eval`
scores the region the same way `score` does, writes nothing to the app, and says for every one
whether a listed spot lands on it, at what rank, and which parts of the score lifted or sank it
(`--json out.json` for the full breakdown). It also checks the bando layer: real derelict buildings
that should have a dot, and parks on old pit sites that shouldn't. Run it before and after a scoring
change.

### New region or new favourite spots

- Regions: add a box to `config/regions.toml` (British National Grid metres, whole km) and list the
  Geofabrik England county extracts that cover it (`osm = ["west-yorkshire"]`); missing extracts are
  downloaded into `data/static/osm/`. Then, for a whole new 40 x 40 km region (~45 min, mostly LiDAR):

  ```bash
  python -m spotfinder lidar --region leeds && python -m spotfinder run --region leeds
  ```

  The whole of England is tiled into 40 km boxes in `config/regions_grid.toml`
  (`python -m spotfinder grid --country england` regenerates it). To work through them unattended:

  ```bash
  python -m spotfinder queue --region all --lidar built --save-to /some/big/disk
  ```

  Add `--jobs 3` to work on three regions side by side in separate processes: the map-data, legal
  and scoring steps are mostly single-threaded, so this is what keeps every core busy (each job gets
  its share of `--workers`). It does each region end to end, fetches the next one while the current one computes, marks
  finished regions with `data/out/<region>/done.json` and skips them on a rerun, so stop and restart
  it whenever. `--lidar built` only streams LiDAR for 1 km squares with 200+ buildings (and their
  neighbours), which is where it matters most; open country uses the tree-height model. About 6 min
  a region that way, ~12 hours for England; `--lidar all` is about twice that.

  `--region` also takes a comma list (`--region durham,leeds`) or `all`. Each `score` rewrites
  `web/public/data/regions.json`, the list the app's region picker reads; `python -m spotfinder regions`
  rewrites just that. `config/app.toml` can narrow it (for now it's Newcastle only: the other regions
  were scored with the old bando and line rules). Open a region in the app with `?region=<key>`.
- Your spots: add them to `config/seeds.toml` with a profile; the ranking looks for places with a
  similar mix of features. A seed doesn't need to be legal for that drone (the Nuns Moor seed isn't,
  for the 5"); it only describes style. Each seed is matched on its own, so a woodland seed and a
  bando seed don't blur into one average.
- Rules and weights: `spotfinder/scoring.py`. Legal distances and land-owner flags:
  `spotfinder/legal.py`.

## How the scoring works

1. **Heights at 1 m**: EA LiDAR first-return surface minus bare earth where available; otherwise
   Meta's tree-height model plus Overture building heights.
2. **Geometry per 1 km tile**: tall = 4 m and up. Gaps are found by closing the tall mask (5 m) and
   keeping the bits that were open: 2-12 m wide, both sides at least 5 m tall, and open ground 8 m out
   on both sides so you can actually fly through (a hole in a wood's canopy doesn't count). Gaps score
   by width and how tall their sides are. Lone trees are tall blobs with at least 80% open ground in a
   10 m ring. Edges are tall pixels touching open ground; tree lines are long, narrow belts of trees
   (40 m+ long, under 15 m wide). With LiDAR, the difference between first and last returns finds
   room under the canopy.
   **Lines**: every fly-through gap, lone tree (orbit), tree 18 m+ (dive) and stretch of tree line
   (skim, one every 25 m) is a node. So is a wall skimmed from the open beside it: for the 5" the edge
   of a wood along a field, skim nodes every 25 m in the open, 3.5-6 m off a wall of trees 8 m+ tall,
   where there's a field beside it (open air 12 m from any tree close by) rather than a ride through the
   wood; for the whoop a building's side too, closer in, 2.5-4.5 m off a wall 6 m+ tall with open air
   8 m out (a square, not an alley). Two nodes link when they're 10-70 m apart (5-35 m for the whoop)
   and a 3 m wide corridor between them is clear below 3 m on the 1 m height grid, flying straight or,
   where a clump of trees is in the way, with one bend round it (off the midpoint by a quarter or 40% of
   the link's length, and worth a little less than flying straight). Only 1 in 12 pairs in range could
   fly straight, so most lines were two or three moves. Trees hanging over a river block a link too
   (heights over water are otherwise zeroed). Before any of this, winter LiDAR crowns are "leafed on":
   1-2 m holes inside a crown are filled to the crown height, and an orbit needs a full 10 m circle of
   clear air round its crown. From each node a greedy walk picks the next obstacle by its value, a
   change of move and a smooth heading (no turns over 115°), up to 12 moves, and scores the run with a
   bonus for variety. Each spot's "flow" is the best run that starts in it, and the spot card gets that
   run as a suggested line, with a take-off on open ground 8 m+ clear of the first obstacle and a
   straight leg to it that keeps off the same homes, car parks, playgrounds, railways and roads and
   doesn't go through a wall, building or viaduct deck (trees it hops over). If the best run has no such
   way in, it's flown backwards, or the next best one is; no line at all if none of the best ten has
   one. The take-off stands on public ground (a park, a common, a beach) if there's some up to 40 m
   further off than the nearest open ground: off that you'd need the landowner's OK. Spots without a
   line get the same: the nearest open ground off gardens, car parks and roads, public if there's some
   within 40 m. The card lists the moves in order, and a skim says what it's along: a tree line, the
   edge of a wood or a clump of trees, or the side of a building.
   Lines keep off people's homes and gardens (residential land that isn't a park or a big green), car
   parks, school, college, university, hospital and prison grounds (playing fields included), children's
   playgrounds (10 m round), working railways and the Metro in the open air (8 m either side: trains,
   and often overhead wires), power lines (15 m either side of a pylon line: the wires hang well out
   from the pylons; 5 m of a wooden-pole one, whose wires are low) and roads (6 m for the 5", 3 m for
   the whoop), and the 5" keeps 12 m from any building; gaps and lone trees in any of those places don't
   count at all. That holds right up to each obstacle: the metres next to a tree line or a tree that a
   link skips for heights (they're the trees themselves) still can't cross a garden or a school field
   beside it, and nor can the take-off leg. A footpath or disused railway viaduct (tagged as one, or a
   bridge called one) whose deck is 8 m+ above the ground has an arch every 15 m: a fly-through gap and
   a move of its own. One still carrying trains (Plessey, Ouseburn, Willington Dene) has none: you'd be
   under a train line.
   Neighbouring arches link under the deck, so a run can weave through up to four of them (out through
   one, round the pier, back through the next), where there's 4 m of open air beside the deck on both
   sides to turn in. `land_1m.tif` holds those homes, car parks, school and hospital grounds,
   playgrounds, railways, power lines and viaduct decks. No spot is ever pinned on school, hospital or
   prison grounds, and a school's playing field isn't public land.
3. **Context** from OSM and Overture: distance to water, the sea and beaches, bridges and viaducts,
   bandos, cliffs and quarries, landmarks, masts and cranes, paths, car parks, fast main roads,
   farmland and public open space; and how busy a place is. A bando is a mapped building or works
   tagged derelict, abandoned, ruined or disused, 100 m² or more, not in a park, churchyard or
   museum, not a monument (scheduled, a castle, fort, chapel or abbey ruin) or in a monument's grounds
   (a priory's outbuildings are often mapped on their own, untagged), and with LiDAR still
   seeing walls 4 m+ tall on its footprint. A bridge counts where LiDAR sees its deck 4 m+ in the air
   for 30 m over water or 80 m anywhere (Overture flags whole road segments, culverts and all);
   railway and footpath viaducts always count (so does a bridge called one: Plessey Viaduct is mapped
   as a plain bridge), and so does a bridge 20 m+ long that's listed or has a Wikidata entry of its own
   (Causey Arch, 24 m up a gorge). Lighthouses, famous artworks, those famous bridges and named
   viaducts are landmarks; the named ones you'd build a shot round (a lighthouse, castle, abbey,
   monument, pier, viaduct or famous bridge) are icons, and count in full for 5" cine where a viewpoint, water tower
   or chimney only counts in part. A ruin or
   monument needs a Wikidata entry or a listing to be an icon: OSM has every war memorial and
   drinking fountain as a monument. Icons that stand up are heroes, a shot on their own for 5" cine
   (the Angel of the North, Penshaw Monument, Prudhoe Castle): a lighthouse, windmill, castle or priory,
   any other icon LiDAR sees 8 m+ tall on its footprint, or a whole site of 2 ha+ (Northumberlandia).
   A beach is mapped sand, shingle or beach of 1,500 m² or more that isn't on a
   golf course (so no bunkers or long-jump pits); for 5" cine only a beach by the sea is "the coast".
   Power lines (within 50 m) and fast roads cost a spot in full only when its middle is on one, and
   otherwise up to half, by how much of it is near one; for 5" cine a road only costs by how much of
   the spot it fills (you film the Angel, not the A167). For 5" cine water counts by how much of the
   frame it fills too: a big river or the sea beats a burn. Golden hour counts a clear view down to the
   sunrise as well as the sunset: the coast here faces east.
   Also worked out per spot: how big the open field is, how far it is to walk in from a path or road,
   and how high the skyline is towards the sunset (west-south-west to west-north-west, out to 2 km,
   hills, woods and buildings included).
4. **Legal grid at 5 m**: A3 needs 150 m from residential, commercial, industrial and recreational
   land (anything named a country park counts, however OSM tags it: Bolam Lake is mapped as a nature
   reserve) and 50 m from buildings and busy roads; flight restriction zones and restricted areas block
   both drones. MOD land and danger areas are never suggested, and nor is anywhere a spot would
   reach onto them (wartime pillboxes and gun emplacements, mapped as military bunkers all along
   the coast, aren't MOD land). National Trust, English Heritage (no recreational take-off or landing
   at all, and they ask you not to fly over), Forestry England, Woodland Trust, nature reserves, golf
   courses, cemeteries, railways (not ones in tunnels) and power lines are flagged. Land that needs the
   owner's OK costs a spot points by how much of the spot is on it, the full amount from half the spot
   on: you can take off beside Penshaw Monument or Tynemouth Priory, not in the middle of a golf course.
5. **Score**: a weighted rule score blended 60/40 with similarity to your seed spots. For the 5",
   public land (parks, commons, country parks, beaches), bridges and viaducts, and bandos most of all
   weigh in, and public land is part of the taste match too: without it the taste part filled the top
   with private fields that happen to look like the Town Moor. Height to dive (tall trees or walls, a
   hill) only counts for the 5" where there's open air beside it: a valley side that's woods all the
   way down is canopy to hop over, not a dive. A bridge or viaduct still carrying trains doesn't count
   for the 5" (lines keep off the tracks, so Hawthorn Dene and Plessey are ones to film, not fly
   round); it does for cine. Bridges, bandos, beaches and landmarks are measured from the middle of the
   spot, so the best cell is on them, not 300 m away in the next field. 5" cine is led by its subject (landmarks, the coast,
   bridges) more than by water, hills and sunsets, which every river valley has, a hero most of all
   (a nameless bend of the Browney no longer outranks the Angel), and being busy only
   costs it a little: you film at first light. Spots are
   suggestions to go and look at, so legality is a label rather than a filter: anywhere outside
   flight restriction zones and away from MOD land can be picked, and each spot says how much of it
   is A3-clear. `python -m spotfinder score --strict` brings back the old behaviour (the 5" only
   gets A3-clear ground). Spots are picked greedily with a minimum spacing (400 m for the 5", 600 m
   for 5" cine, 250 m for the whoop). When a viaduct, bridge, landmark, bando or beach is within 85% of
   that and scores at most 6 points less, the pin goes on it, a stronger subject pulling a little
   harder (for 5" cine: an icon, then a bridge, a bando, a beach).
   Each spot carries a "why" list: points out of 100 per reason, with the numbers behind it (e.g.
   "14 fly-through gaps", "361 m of tree line to skim", "820 m from the nearest path or road").
   Spots that share a name and a place say which part of it they're in, from where they lie against
   each other: "Riverside Park (south)", "(middle)", "(north)".
6. **Parks**: the ranking alone favours woods and riversides out of town, so every named park,
   common or recreation ground of 4 ha or more that has no spot yet gets one at its best place
   (listed after the top 250), and every spot inside a park is labelled with it.
   The app's "Parks" chip lists them all.
7. **3D models**: every spot gets a 320 m square model at 2 m from the same heights (about 20 KB
   each); `score --previews N` limits them to the top N per drone. A bridge or viaduct mapped in OSM
   is a deck in the air with open air under it, over a river too, and "Fly it" flies under it.
8. **Bandos**: `bandos.geojson` has every bando as a point with what it was (from its OSM tags),
   how tall its walls still stand (LiDAR, 95th percentile on the footprint, tree crowns left out) and
   how much roof it has. Buildings of one site (a depot's sheds, a works) are one point. Each gets a 3D
   close-up (160 m square at 1 m) that opens from its card.
9. **Shots** (5" cine spots with a 3D model): the subject is the sight the spot is named after, else a
   named sea stack or rock within 250 m, else cliffs within 100 m (OSM's pieces joined up). A bridge or
   viaduct gets a pass, the longest clear stretch (60 m or more) of the lines 25, 35 or 50 m off either
   side of its deck, flown at deck height; cliffs get one the same way, level with their top and
   following their curve. Anything else gets an orbit 25 to 70 m out from its edge and 5 m over its
   top: the closest ring with half a turn clear, or at least a quarter. Both are flown level and keep
   off the ground a line keeps off, with the ground (from the DTM) and everything on it at least 5 m
   under them, so a viaduct's pass is the stretch over its valley and an orbit doesn't cut into the
   hillside behind a monument. A subject over 120 m across (a castle's whole grounds) gets none.

## Be real about it

- It knows what the data knows. Fences, "no drones" signs, livestock, nesting birds, crowds on the
  day and private land aren't in it. Go and look first, and take a spotter when you fly in goggles.
- Land use and land owners come from OpenStreetMap. If a park or a National Trust boundary isn't
  mapped, it isn't known here. Chopwell Wood (Forestry England) and Penshaw Monument (National Trust)
  have no operator tag in OSM, so they're listed by hand in `legal.KNOWN_OWNERS`; adding `operator=*`
  on the map is the better fix, and other untagged land isn't flagged.
- Without LiDAR, trees under ~4 m and narrow gaps get missed and building heights are estimates.
- The A3 check is distance-based and a bit conservative around edges (20 m pixels). The "can I fly
  here" verdict is a planning aid, not legal advice: check the CAA Drone Code and a drone safety map
  app before you fly.
- Wind limits for the flyable/not strip are a judgement call (gusts under ~28 mph for the 5", ~12 mph
  for the whoop).

## Data and licences

OpenStreetMap contributors (ODbL), Overture Maps Foundation (CDLA-Permissive / ODbL layers),
Meta & World Resources Institute canopy height (CC BY 4.0), Environment Agency LiDAR (Open Government
Licence), NATS UAS restriction zones, AWS Terrain Tiles (various open sources), Open-Meteo (CC BY 4.0).
