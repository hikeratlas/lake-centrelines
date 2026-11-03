import io
import json
import os
import sys

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import Point, Polygon, shape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import lake_centrelines as lc  # noqa: E402


def lonlat_poly(merc_poly):
    return shapely.transform(merc_poly, lc.lonlat)


def feature(geom, fid='a10', **tags):
    return json.dumps({'type': 'Feature', 'id': fid, 'properties': tags,
                       'geometry': shapely.geometry.mapping(geom)})


def test_is_lake():
    assert lc.is_lake({'natural': 'water'})
    assert lc.is_lake({'natural': 'water', 'water': 'reservoir'})
    assert lc.is_lake({'natural': 'bay'})
    assert lc.is_lake({'natural': 'strait'})
    assert not lc.is_lake({'natural': 'water', 'water': 'river'})
    assert not lc.is_lake({'natural': 'water', 'water': 'canal'})
    assert not lc.is_lake({'landuse': 'reservoir'})


def test_osm_ref():
    assert lc.osm_ref('a10') == ('way', 5)
    assert lc.osm_ref('a11') == ('relation', 5)
    assert lc.osm_ref('w5') == (None, None)
    assert lc.osm_ref(None) == (None, None)


def test_long_lake_runs_along_its_length():
    # A 20 x 2 km ellipse, long axis east-west, near 50°N
    x0, y0 = lc.merc(np.array([[-120.0, 50.0]]))[0]
    lake = shapely.affinity.scale(Point(x0, y0).buffer(1000, 64), 10, 1)
    out = json.loads(lc.centreline(feature(lonlat_poly(lake), name='Long Lake', natural='water')))
    assert out['properties'] == {'osm_type': 'way', 'osm_id': 5, 'name': 'Long Lake'}
    line = shapely.transform(shape(out['geometry']), lc.merc)
    (minx, miny, maxx, maxy) = line.bounds
    assert maxx - minx > 14000         # most of the 20 km length
    assert maxy - miny < 1000          # and roughly straight along it
    assert lake.buffer(1).contains(line)


def test_bent_lake_prefers_a_straight_arm():
    # An L of two 10 x 1 km arms. Of the long skeleton paths, the least
    # curved wins, so the label runs straight along one arm rather than
    # round the corner.
    x0, y0 = lc.merc(np.array([[-120.0, 50.0]]))[0]
    lake = Polygon([(x0, y0), (x0 + 10000, y0), (x0 + 10000, y0 + 10000), (x0 + 9000, y0 + 10000),
                    (x0 + 9000, y0 + 1000), (x0, y0 + 1000)])
    out = json.loads(lc.centreline(feature(lonlat_poly(lake), name='Bent Lake', natural='water')))
    line = shapely.transform(shape(out['geometry']), lc.merc)
    assert lake.buffer(1).contains(line)
    minx, miny, maxx, maxy = line.bounds
    # along one arm: long one way, about the arm's width the other
    assert max(maxx - minx, maxy - miny) > 8000
    assert min(maxx - minx, maxy - miny) < 1000


def test_skips():
    x0, y0 = lc.merc(np.array([[-120.0, 50.0]]))[0]
    big = lonlat_poly(shapely.affinity.scale(Point(x0, y0).buffer(1000, 32), 10, 1))
    small = lonlat_poly(Point(x0, y0).buffer(500, 32))  # < 2 km²
    assert lc.centreline(feature(big, natural='water')) is None                      # no name
    assert lc.centreline(feature(big, name='R', natural='water', water='river')) is None
    assert lc.centreline(feature(small, name='Pond', natural='water')) is None
    assert lc.centreline('') is None


def test_largest_part_of_a_multipolygon():
    x0, y0 = lc.merc(np.array([[-120.0, 50.0]]))[0]
    a = shapely.affinity.scale(Point(x0, y0).buffer(1000, 32), 10, 1)
    b = Point(x0, y0 + 50000).buffer(1500, 32)
    mp = shapely.geometry.MultiPolygon([a, b])
    out = json.loads(lc.centreline('\x1e' + feature(lonlat_poly(mp), fid='a11', name='Twin', natural='water')))
    assert out['properties']['osm_type'] == 'relation'
    line = shapely.transform(shape(out['geometry']), lc.merc)
    assert a.buffer(1).contains(line)


def test_run_end_to_end():
    x0, y0 = lc.merc(np.array([[-120.0, 50.0]]))[0]
    lake = lonlat_poly(shapely.affinity.scale(Point(x0, y0).buffer(1000, 32), 10, 1))
    src = io.StringIO('\x1e' + feature(lake, name='A', natural='water') + '\n'
                      + '\x1e' + feature(lake, name='B', natural='water', water='river') + '\n')
    out = io.StringIO()
    assert lc.run(src, out, lc.Options(), processes=1) == 1
    assert json.loads(out.getvalue())['properties']['name'] == 'A'
