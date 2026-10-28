#!/bin/sh
set -eu
cd /opt/ostrovok
mkdir -p data/source data/extract data/overpass .cache/server
# City and surroundings, including airport. bbox order: west,south,east,north.
BBOX=48.75,55.55,49.55,56.10
if [ ! -s data/extract/kazan.osm.pbf ]; then
  if [ ! -s data/source/region.osm.pbf ]; then
    if ! curl --silent --show-error --fail --location --retry 2 --connect-timeout 15 --max-time 180 \
      https://download.openstreetmap.fr/extracts/russia/volga_federal_district/tatarstan_republic-latest.osm.pbf \
      -o data/source/region.osm.pbf.part; then
      curl --silent --show-error --fail --location --retry 3 --connect-timeout 15 \
        https://download.geofabrik.de/russia/volga-fed-district-latest.osm.pbf \
        -o data/source/region.osm.pbf.part
    fi
    mv data/source/region.osm.pbf.part data/source/region.osm.pbf
  fi
  osmium extract --bbox "$BBOX" --strategy smart \
    data/source/region.osm.pbf -o data/extract/kazan.osm.pbf.part --output-format pbf --overwrite
  mv data/extract/kazan.osm.pbf.part data/extract/kazan.osm.pbf
fi
osmium fileinfo -e data/extract/kazan.osm.pbf > data/extract/fileinfo.txt
sha256sum data/extract/kazan.osm.pbf > data/extract/SHA256SUMS
# The API runs as a non-root user inside Docker.
sudo chown -R 10001:10001 .cache/server
