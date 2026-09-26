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

# The web application is deliberately NOT copied here. It is useless without
# the API behind it -- it would render a login box that can never authenticate
# -- and publishing a sign-in page that cannot work is worse than publishing
# nothing. `terrashield serve` is what serves it, from the same process as the
# API it talks to.

# Findings about real named places, produced from modelled imagery. Not
# something to hand to a crawler.
cat > public/robots.txt <<'ROBOTS'
User-agent: *
Disallow: /
ROBOTS

echo "public/ assembled:"
find public -type f | sort | sed 's/^/  /' | head -20
echo "  ... $(find public -type f | wc -l) files total"
