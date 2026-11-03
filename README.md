# lake-centrelines

Lines through the middle of lakes, bays and straits in OpenStreetMap, for labelling them along their length the way printed maps do. A map style draws the name with `symbol-placement: line` along the centreline, instead of at a single point.

![Adams Lake, BC, labelled three ways. At a point, the horizontal name runs mostly over land. Along the shoreline, it sits half on land, and MapLibre only draws it with its angle limit off. Along the centreline, it sits in the water and follows the lake's bend.](docs/comparison.png)

*Adams Lake, British Columbia, drawn with MapLibre. The right panel uses this repo's output. Map data © OpenStreetMap contributors.*

Input is an OSM PBF and output is GeoJSON lines: one LineString per named lake, in longitude/latitude. All of North America takes about 13 minutes on a 4-core machine (see [Speed](#speed)).

[Hiker Atlas](https://www.hikeratlas.com/map) uses it for its basemap ([hikeratlas/basemap](https://github.com/hikeratlas/basemap)).

## Usage

You need [osmium-tool](https://osmcode.org/osmium-tool/), and Python 3.10 or later with numpy, scipy and shapely 2:

```sh
sudo apt-get install osmium-tool          # or: brew install osmium-tool
python3 -m venv venv && venv/bin/pip install -r requirements.txt

PYTHON=venv/bin/python ./lake-centrelines.sh british-columbia-latest.osm.pbf lakes.geojsonl
```

`lake-centrelines.sh` cuts the named water areas out of the PBF with osmium, then runs `lake_centrelines.py` on them. That second step also works on its own, on any GeoJSON lines of polygons with OSM tags as properties (`-` reads stdin or writes stdout):

```sh
osmium export -f geojsonseq --geometry-types=polygon --add-unique-id=type_id -o water.geojsonseq water.pbf
venv/bin/python lake_centrelines.py water.geojsonseq lakes.geojsonl
```

Each output line is a feature like:

```json
{"type":"Feature","properties":{"osm_type":"relation","osm_id":13321885,"name":"Strait of Georgia"},"geometry":{"type":"LineString","coordinates":[[-124.97,49.84],…]}}
```

`osm_type` and `osm_id` come from osmium's `--add-unique-id=type_id` ids, and are left out if the input has other ids.

### Which areas get a line

- **Tags:** named areas tagged `natural=bay`, `natural=strait`, or `natural=water` that is either untagged or `water=lake`, `reservoir`, `lagoon`, `stream_pool`, `pond` or `oxbow`. Rivers, canals and the like are excluded, since they're labelled along the river instead.
- **Size:** at least 2 km², measured in Web Mercator (as osm-lakelines does). Because Web Mercator stretches areas away from the equator, the cut-off is effectively smaller further north.
- **Multipolygons:** only the largest part of a multipolygon gets a line, so each lake gets one label.

### Options

| Option | Default | |
|---|---|---|
| `--min-area` | 2000000 | smallest area to label, m² in Web Mercator |
| `--simplify` | 25 | simplify the outline first, m |
| `--segment` | 100 | spacing of the outline points the skeleton is built from, m |
| `--max-points` | 10000 | most outline points per area; the spacing grows to fit |
| `--max-paths` | 5 | long skeleton paths to choose the straightest from |
| `--sigma` | 3 | gaussian smoothing of the line, in skeleton vertices |
| `--processes` | one per CPU | worker processes |

## How it works

For each area, in Web Mercator metres:

1. **Simplify and resample the outline.** Simplify it by 25 m, then put points every 100 m along its outer ring. If that's more than 10,000 points, the spacing grows until it fits.
2. **Build the skeleton.** Compute the Voronoi diagram of those points. Its edges that lie inside the lake form a skeleton of the lake's shape (approximately its medial axis). Only the largest connected piece is kept.
3. **Find long paths.** Find the longest paths between the skeleton's end nodes (see [Speed](#speed)).
4. **Choose and smooth.** Of the 5 longest paths, take the least curved, i.e. the one with the smallest total turning. Smooth it with a gaussian filter. Preferring the straightest path is deliberate: on an L-shaped lake the label runs straight along one arm rather than bending round the corner.

## Provenance

This is a rewrite of a long-standing OpenMapTiles approach.

- **[lukasmartinelli/osm-lakelines](https://github.com/lukasmartinelli/osm-lakelines)** (Lukas Martinelli, 2016, MIT) built lake centrelines for [OpenMapTiles](https://openmaptiles.org/)' `water_name` layer. It imported OSM lakes into PostGIS with imposm, then ran label_centerlines on each.
- **[acalcutt/osm-lakelines](https://github.com/acalcutt/osm-lakelines)** (2021–22, MIT) is a maintained fork that published planet-wide releases. Hiker Atlas first prototyped with its v12 release (April 2022). That release has no lines for lakes mapped since 2022, and none for straits.
- **[ungarj/label_centerlines](https://github.com/ungarj/label_centerlines)** (EOX IT Services, 2015–18, MIT) is the centreline algorithm itself: the Voronoi skeleton, the longest paths, the least curved of them, and smoothing.

`lake_centrelines.py` reimplements label_centerlines' algorithm with osm-lakelines' settings. osm-lakelines selected and simplified lakes with:

```sql
SELECT osm_id, ST_SimplifyPreserveTopology(geometry, 25) AS geometry FROM osm_lake_polygon WHERE area > 2 * 1000 * 1000 AND name <> ''
```

Then it ran label_centerlines with:

```
label_centerlines --max_points=10000 --simplification=0.02 --smooth=3 --max_paths=5 --segmentize_maxlen=100
```

`--max-points`, `--max-paths`, `--sigma` and `--segment` here match the label_centerlines flags.

There are three differences from that pipeline:
- **Too many points:** for an outline over the point limit, label_centerlines simplifies it in steps of `--simplification`. This version widens the point spacing instead, which keeps the points evenly spaced.
- **Multipolygons:** label_centerlines draws a line in every part of a multipolygon. This version draws one only in the largest part, so each lake gets one label.
- **No database:** osmium filters and streaming GeoJSON replace the PostGIS/imposm step. Compared with acalcutt's April 2022 release for the same lakes, its lines lie on top of the release's, checked on Trout, Okanagan, Kootenay, Babine, Atlin and Harrison lakes in British Columbia.

## Speed

label_centerlines was far too slow to run on a large region. It ran for over 14 minutes on British Columbia without finishing. With the rewrite, all of British Columbia takes 4–7 s, and North America takes about 3 minutes once the water is extracted.

**Where the time went:** profiling one lake, Trout Lake in BC (45 s), showed 90% of the time in shortest-path searches. To find the longest paths, label_centerlines runs a separate networkx `single_source_dijkstra` search between every pair of the skeleton's end nodes. A skeleton has an end node at every little spur, so that was about 4,900 searches for Trout Lake, and far more for a big lake like Williston: the number of pairs grows with the square of the number of spurs. It also built the graph in Python, testing each Voronoi edge against the lake one at a time.

**What changed:**

- **Long paths from three searches instead of one per pair of end nodes:** the longest path in a tree runs from the node farthest from any start (a) to the node farthest from a (b). The skeleton is a tree, except around islands. So this runs one search to find a, one from a, and one from b, and takes the candidate paths from the last two. It uses scipy's compiled `csgraph.dijkstra` on a sparse graph.
- **Vectorised geometry:** one `shapely.contains_xy` call over all Voronoi vertices against the prepared polygon, and `shapely.transform` with numpy for the projections, instead of per-edge and per-point Python.
- **NumPy 2:** label_centerlines (2022) uses `np.math`, which NumPy 2 removed.
- **Processes:** areas are processed in parallel, and streamed from input to output rather than loaded all at once.

**Measured:**

| | Before (label_centerlines) | After |
|---|---|---|
| Trout Lake, BC | 45 s | 0.02 s |
| British Columbia: 47,164 named water areas → 2,979 lines | over 14 min, unfinished | 4–7 s, plus 33 s of osmium |
| North America (GitHub `ubuntu-24.04-arm`, 4 vCPUs) | – | 175 s for 37,619 lines, plus about 10 min of osmium (545,200 named water areas) |

The North America run gives 37,619 lines, where acalcutt's April 2022 release had 19,053 for North America. The difference is lakes mapped since 2022, plus bays and straits.

## Limits

- **Lakes with many islands** get a line along only part of their length, e.g. Babine, Atlin and Harrison in BC. The islands split the skeleton into pieces and only the largest piece is used. osm-lakelines has the same limit.
- **Very large or intricate areas** keep at most 10,000 outline points. Their skeleton is coarser, which is fine for a label line.

## Tests

```sh
venv/bin/pip install pytest && venv/bin/pytest tests
```

## Licence

MIT. See [LICENSE](LICENSE), which includes the copyright notices of label_centerlines and osm-lakelines. The output is derived from OpenStreetMap data, © OpenStreetMap contributors, under the [ODbL](https://www.openstreetmap.org/copyright).
