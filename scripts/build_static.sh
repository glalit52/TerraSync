#!/usr/bin/env bash
# Assemble the static site.
#
# Deliberately a copy, not a rebuild. Regenerating the console means running
# months of the monitoring pipeline across four sites, which takes far longer
# than a deploy should and would make the published page depend on whatever the
# build container happened to have. The page is built and reviewed locally,
# committed, and shipped as-is.
set -euo pipefail

rm -rf public
mkdir -p public/terrashield

cp dashboard/index.html public/terrashield/index.html
cp dashboard/data.json  public/terrashield/data.json
if [ -d dashboard/chips ]; then
  cp -r dashboard/chips public/terrashield/chips
fi

# The console is the whole site for now, so send the root at it.
cp dashboard/index.html public/index.html
if [ -d dashboard/chips ]; then
  cp -r dashboard/chips public/chips
fi

# Findings about real named places, produced from modelled imagery. Not
# something to hand to a crawler.
cat > public/robots.txt <<'ROBOTS'
User-agent: *
Disallow: /
ROBOTS

echo "public/ assembled:"
find public -type f | sort | sed 's/^/  /' | head -20
echo "  ... $(find public -type f | wc -l) files total"
