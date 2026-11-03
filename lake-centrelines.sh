#!/bin/bash
# OSM PBF in, lake centrelines (GeoJSON lines) out.
#
#   lake-centrelines.sh IN.osm.pbf OUT.geojsonl [lake_centrelines.py options]
#
# Needs osmium-tool, and Python with numpy, scipy and shapely (see
# requirements.txt). Intermediate files go in a temporary directory, or in
# $TMPDIR if set.
set -euo pipefail

if [ $# -lt 2 ]; then
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
in=$1 out=$2
shift 2
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

start=$(date +%s)
# The areas lake_centrelines.py keeps (see is_lake), then only named ones:
# unnamed water is most of it, and gets no label.
osmium tags-filter --overwrite -o "$tmp/water.pbf" "$in" \
  'a/natural=water,bay,strait' 'a/water=lake,reservoir,lagoon,stream_pool,pond,oxbow'
osmium tags-filter --overwrite -o "$tmp/named.pbf" "$tmp/water.pbf" 'a/name'
osmium export --overwrite -f geojsonseq --geometry-types=polygon --add-unique-id=type_id \
  -o "$tmp/water.geojsonseq" "$tmp/named.pbf"
echo "Extracted $(wc -l < "$tmp/water.geojsonseq") named water areas in $(( $(date +%s) - start ))s" >&2

"${PYTHON:-python3}" "$here/lake_centrelines.py" "$tmp/water.geojsonseq" "$out" "$@"
