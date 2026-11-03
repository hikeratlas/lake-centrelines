#!/usr/bin/env python3
"""Centrelines for labelling lakes, bays and straits along their length.

Reads water areas as GeoJSON lines (one feature per line, e.g. from
`osmium export -f geojsonseq`) and writes one centreline per named area as
GeoJSON lines. See README.md for the method and where it comes from.

    lake_centrelines.py IN.geojsonseq OUT.geojsonl [options]

Use - for stdin or stdout.
"""
import argparse
import json
import multiprocessing
import sys
import time

import numpy as np
import shapely
from scipy.ndimage import gaussian_filter1d
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import Voronoi
from shapely.geometry import LineString, mapping, shape

R = 6378137.0  # Web Mercator sphere radius, m

# Tag filter: which named areas get a centreline.
LAKE_NATURAL = {'bay', 'strait'}
LAKE_WATER = {'lake', 'reservoir', 'lagoon', 'stream_pool', 'pond', 'oxbow'}


def is_lake(tags):
    """True for the water areas worth labelling along a line.

    natural=bay and natural=strait, and natural=water that is untagged or
    tagged water=lake/reservoir/lagoon/stream_pool/pond/oxbow (not rivers,
    canals, basins and the like, which are labelled differently).
    """
    if tags.get('natural') in LAKE_NATURAL:
        return True
    w = tags.get('water')
    if w is not None:
        return w in LAKE_WATER
    return tags.get('natural') == 'water'


def merc(c):
    """lon/lat -> Web Mercator metres (for shapely.transform)."""
    return np.column_stack((R * np.radians(c[:, 0]),
                            R * np.log(np.tan(np.pi / 4 + np.radians(c[:, 1]) / 2))))


def lonlat(c):
    """Web Mercator metres -> lon/lat, rounded to 6 places (~10 cm)."""
    return np.round(np.column_stack((np.degrees(c[:, 0] / R),
                                     np.degrees(2 * np.arctan(np.exp(c[:, 1] / R)) - np.pi / 2))), 6)


def path_to(pred, end):
    path = [end]
    while pred[path[-1]] >= 0:
        path.append(pred[path[-1]])
    return path


def farthest_ends(graph, src, ends, n):
    """The n end nodes farthest from src (reachable ones only), with paths."""
    dist, pred = dijkstra(graph, directed=False, indices=src, return_predecessors=True)
    d = np.where(np.isinf(dist[ends]), -1, dist[ends])
    return [(d[i], path_to(pred, ends[i])) for i in np.argsort(-d)[:n] if d[i] > 0]


def longest_paths(graph, ends, n):
    """Up to n long paths between end nodes of the skeleton, longest first.

    The longest path runs from the end node farthest from any node (a) to
    the end node farthest from a (b); that's exact for a tree, and the
    skeleton is one except around islands. The other candidates are the
    next farthest end nodes from a and from b. This takes three
    single-source searches, where label_centerlines searches from every end
    node to every other.
    """
    a = farthest_ends(graph, ends[0], ends, 1)[0][1][0]
    from_a = farthest_ends(graph, a, ends, n)
    b = from_a[0][1][0]
    cands = sorted(from_a + farthest_ends(graph, b, ends, n), key=lambda c: -c[0])
    return [p for _, p in cands[:n]]


def angles_sum(pts):
    """Total absolute turning along a path, radians."""
    v = np.diff(pts, axis=0)
    a = np.arctan2(v[:, 1], v[:, 0])
    return np.abs((np.diff(a) + np.pi) % (2 * np.pi) - np.pi).sum()


def skeleton_line(poly, seg, max_points=10000, max_paths=5, sigma=3):
    """A smoothed centreline of a Polygon, or None.

    Voronoi diagram of points along the outline; the edges inside the
    polygon form its skeleton. Of the skeleton's long end-to-end paths, take
    the least curved, then smooth it.
    """
    outline = shapely.segmentize(poly.exterior, seg)
    while len(outline.coords) > max_points:
        seg *= 1.5
        outline = shapely.segmentize(poly.exterior.simplify(seg / 4), seg)
    pts = np.asarray(outline.coords)[:-1]
    vor = Voronoi(pts)
    rv = np.asarray(vor.ridge_vertices)
    rv = rv[(rv >= 0).all(axis=1)]
    shapely.prepare(poly)
    inside = shapely.contains_xy(poly, vor.vertices[:, 0], vor.vertices[:, 1])
    rv = rv[inside[rv[:, 0]] & inside[rv[:, 1]]]
    if len(rv) < 2:
        return None
    w = np.linalg.norm(vor.vertices[rv[:, 0]] - vor.vertices[rv[:, 1]], axis=1)
    nv = len(vor.vertices)
    graph = coo_matrix((w, (rv[:, 0], rv[:, 1])), shape=(nv, nv)).tocsr()
    graph = graph + graph.T
    # Only the largest connected piece of the skeleton.
    _, comp = connected_components(graph, directed=False)
    used = np.unique(rv)
    main = np.bincount(comp[used]).argmax()
    deg = np.diff(graph.indptr)
    ends = np.flatnonzero((deg == 1) & (comp == main))
    if len(ends) < 2:
        return None
    paths = longest_paths(graph, ends, max_paths)
    if not paths:
        return None
    best = min((vor.vertices[p] for p in paths), key=angles_sum)
    if len(best) > 2 * sigma:
        best = np.column_stack((gaussian_filter1d(best[:, 0], sigma), gaussian_filter1d(best[:, 1], sigma)))
    return LineString(best)


def osm_ref(feature_id):
    """osmium's --add-unique-id=type_id ids: an area from way n is a(2n),
    from relation n is a(2n+1). Returns (osm_type, osm_id), or (None, None)."""
    if isinstance(feature_id, str) and feature_id[:1] == 'a' and feature_id[1:].isdigit():
        n = int(feature_id[1:])
        return ('relation' if n % 2 else 'way'), n // 2
    return None, None


class Options:
    min_area = 2_000_000   # m², in Web Mercator
    simplify = 25          # m
    segment = 100          # m
    max_points = 10000
    max_paths = 5
    sigma = 3
    slow = 10              # s; log areas slower than this


OPTS = Options()


def centreline(line):
    """One input line -> one output line, or None."""
    line = line.lstrip('\x1e').strip()
    if not line:
        return None
    feat = json.loads(line)
    tags = feat.get('properties') or {}
    if not tags.get('name') or not is_lake(tags) or not feat.get('geometry'):
        return None
    g = shape(feat['geometry'])
    if g.geom_type not in ('Polygon', 'MultiPolygon'):
        return None
    gm = shapely.transform(g, merc)
    # Label the largest part: one label per lake.
    if gm.geom_type == 'MultiPolygon':
        gm = max(gm.geoms, key=lambda p: p.area)
    if gm.area < OPTS.min_area:
        return None
    gm = gm.simplify(OPTS.simplify, preserve_topology=True)
    if gm.is_empty or gm.geom_type != 'Polygon':
        return None
    t = time.time()
    try:
        cl = skeleton_line(gm, OPTS.segment, OPTS.max_points, OPTS.max_paths, OPTS.sigma)
    except Exception as e:
        print(f'{feat.get("id")} {tags["name"]}: {type(e).__name__}: {e}', file=sys.stderr)
        return None
    if cl is None:
        return None
    if time.time() - t > OPTS.slow:
        print(f'{feat.get("id")} {tags["name"]}: {time.time() - t:.0f}s', file=sys.stderr)
    osm_type, osm_id = osm_ref(feat.get('id'))
    props = {'osm_type': osm_type, 'osm_id': osm_id, 'name': tags['name']} if osm_type else {'name': tags['name']}
    geom = mapping(shapely.transform(cl, lonlat))
    return json.dumps({'type': 'Feature', 'properties': props, 'geometry': geom},
                      separators=(',', ':'), ensure_ascii=False)


def _init(opts):
    global OPTS
    OPTS = opts


def run(src, out, opts, processes=None):
    n = 0
    with multiprocessing.Pool(processes, initializer=_init, initargs=(opts,)) as pool:
        for r in pool.imap_unordered(centreline, src, chunksize=16):
            if r:
                out.write(r + '\n')
                n += 1
    return n


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('input', help='GeoJSON lines of water areas (- for stdin)')
    p.add_argument('output', help='GeoJSON lines of centrelines (- for stdout)')
    d = Options
    p.add_argument('--min-area', type=float, default=d.min_area, help=f'smallest area to label, m² in Web Mercator (default {d.min_area:g})')
    p.add_argument('--simplify', type=float, default=d.simplify, help=f'simplify the outline first, m (default {d.simplify:g})')
    p.add_argument('--segment', type=float, default=d.segment, help=f'spacing of outline points for the skeleton, m (default {d.segment:g})')
    p.add_argument('--max-points', type=int, default=d.max_points, help=f'most outline points per area (default {d.max_points})')
    p.add_argument('--max-paths', type=int, default=d.max_paths, help=f'long skeleton paths to choose from (default {d.max_paths})')
    p.add_argument('--sigma', type=float, default=d.sigma, help=f'gaussian smoothing, in skeleton vertices (default {d.sigma:g})')
    p.add_argument('--processes', type=int, default=None, help='worker processes (default: one per CPU)')
    a = p.parse_args(argv)
    opts = Options()
    for k in ('min_area', 'simplify', 'segment', 'max_points', 'max_paths', 'sigma'):
        setattr(opts, k, getattr(a, k))
    t = time.time()
    src = sys.stdin if a.input == '-' else open(a.input, encoding='utf-8')
    out = sys.stdout if a.output == '-' else open(a.output, 'w', encoding='utf-8')
    with src, out:
        n = run(src, out, opts, a.processes)
    print(f'{n} centrelines in {time.time() - t:.0f}s', file=sys.stderr)


if __name__ == '__main__':
    main()
