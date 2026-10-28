#!/bin/sh
set -eu
cd /opt/ostrovok
mkdir -p data/source data/extract data/overpass-agglomeration-v1 .cache/server
# Agglomeration, including regional bridge detours. west,south,east,north.
BBOX=48.20,55.30,49.85,56.35
osmium_run() {
  if command -v osmium >/dev/null 2>&1; then
    osmium "$@"
  else
    docker run --rm --entrypoint osmium -v "$PWD/data:/work/data" -w /work \
      wiktorn/overpass-api:0.7.62 "$@"
  fi
}
if [ ! -s data/extract/agglomeration-v1.osm.pbf ]; then
  if [ ! -s data/source/volga.osm.pbf ]; then
    curl --silent --show-error --fail --location --retry 3 --connect-timeout 15 \
      https://download.geofabrik.de/russia/volga-fed-district-latest.osm.pbf \
      -o data/source/volga.osm.pbf.part
    mv data/source/volga.osm.pbf.part data/source/volga.osm.pbf
  fi
  osmium_run extract --bbox "$BBOX" --strategy complete_ways \
    data/source/volga.osm.pbf -o data/extract/agglomeration-v1.osm.pbf.part --output-format pbf --overwrite
  osmium_run check-refs data/extract/agglomeration-v1.osm.pbf.part --input-format pbf
  mv data/extract/agglomeration-v1.osm.pbf.part data/extract/agglomeration-v1.osm.pbf
fi
osmium_run fileinfo -e data/extract/agglomeration-v1.osm.pbf > data/extract/agglomeration-v1.fileinfo.txt
sha256sum data/extract/agglomeration-v1.osm.pbf > data/extract/agglomeration-v1.SHA256SUMS
# The API runs as a non-root user inside Docker.
sudo chown -R 10001:10001 .cache/server
