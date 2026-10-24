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
  edges, tallest thing, drop, distance to water/bando/bridge), a suggested take-off point, the
  legal picture for that drone, land-owner warnings, sunrise/golden hour, and an 18-hour wind and
  rain strip from Open-Meteo.
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
  sunset spots and so on, or to your 👍 spots. ‹ › on a spot (or ← →) steps through the list. Each
  spot takes your own notes (parking, access, when it's quiet); they show as 📝 in the list, are
  searchable, and go in the ratings download.
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

Python 3.11+.

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

### New region or new favourite spots

- Regions: add a box to `config/regions.toml` (British National Grid metres, whole km).
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
3. **Context** from OSM and Overture: distance to water, bridges and viaducts, bandos (ruins,
   abandoned/disused structures), cliffs and quarries, landmarks, masts and cranes, paths, car parks,
   fast main roads, farmland and public open space; and how busy a place is. A mapped bando only counts
   in full if LiDAR shows something solid still standing there; old pit and shaft markers with nothing
   left count a little. Town-wall towers, gates and abbeys are landmarks, not bandos.
   Also worked out per spot: how big the open field is, how far it is to walk in from a path or road,
   and how high the skyline is towards the sunset (west-south-west to west-north-west, out to 2 km,
   hills, woods and buildings included).
4. **Legal grid at 5 m**: A3 needs 150 m from residential, commercial, industrial and recreational
   land and 50 m from buildings and busy roads; flight restriction zones and restricted areas block
   both drones. MOD land and danger areas are never suggested, and nor is anywhere a spot would
   reach onto them. National Trust, Forestry England, Woodland Trust, nature reserves, golf courses,
   cemeteries, railways (not ones in tunnels) and power lines are flagged.
5. **Score**: a weighted rule score blended 60/40 with similarity to your seed spots. Spots are
   suggestions to go and look at, so legality is a label rather than a filter: anywhere outside
   flight restriction zones and away from MOD land can be picked, and each spot says how much of it
   is A3-clear. `python -m spotfinder score --strict` brings back the old behaviour (the 5" only
   gets A3-clear ground). Spots are picked greedily with a minimum spacing (400 m for the 5", 600 m
   for 5" cine, 250 m for the whoop).
   Each spot carries a "why" list: points out of 100 per reason, with the numbers behind it (e.g.
   "14 fly-through gaps", "361 m of tree line to skim", "820 m from the nearest path or road").
6. **Parks**: the ranking alone favours woods and riversides out of town, so every named park,
   common or recreation ground of 4 ha or more that has no spot yet gets one at its best place
   (listed after the top 250), and every spot inside a park is labelled with it.
   The app's "Parks" chip lists them all.
7. **3D models**: every spot gets a 320 m square model at 2 m from the same heights (about 20 KB
   each); `score --previews N` limits them to the top N per drone.

## Be real about it

- It knows what the data knows. Fences, "no drones" signs, livestock, nesting birds, crowds on the
  day and private land aren't in it. Go and look first, and take a spotter when you fly in goggles.
- Land use and land owners come from OpenStreetMap. If a park or a National Trust boundary isn't
  mapped, it isn't known here. Chopwell Wood, for example, has no operator tag in OSM, so it isn't
  flagged as Forestry England land.
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
