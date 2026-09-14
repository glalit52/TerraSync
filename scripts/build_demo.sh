#!/usr/bin/env bash
# Build the committed TerraShield demo database.
#
# Each site gets its own monitoring window, chosen to span that site's
# scripted events. That is not a shortcut around a single long run -- it is
# what a real estate looks like. Sites are enrolled when a customer buys them,
# so an estate has four different start dates and four different amounts of
# history, and every number the console shows has to survive that. It is why
# coverage is measured from each site's first acquisition rather than from a
# nominal window, and why baselines are per AOI.
#
#   IN-BHD-SOLAR   an array block is commissioned on 2026-03-19
#   IN-MUN-PORT    a warehouse appears 2026-04-22; congestion 2026-05-11..26
#   IN-KCH-SECTOR  a track extends 2026-07-09, two structures follow, and
#                  optical is cloud-bound throughout -- the SAR demonstration
#   IN-SSD-DAM     the reservoir draws down from 2026-04-07
set -euo pipefail

DB="${1:-terrashield.db}"
ACTOR="${TERRASHIELD_ACTOR:-demo@sensegrass.com}"
TS=(terrashield --db "$DB" --actor "$ACTOR")

rm -f "$DB"
"${TS[@]}" init

monitor() {
  local site="$1" id="$2" from="$3" to="$4"
  "${TS[@]}" enroll "$site"
  "${TS[@]}" monitor "$id" --from "$from" --to "$to"
}

monitor bhadla IN-BHD-SOLAR  2026-03-01 2026-04-15
monitor mundra IN-MUN-PORT   2026-04-10 2026-05-31
monitor kutch  IN-KCH-SECTOR 2026-07-01 2026-08-20
monitor sardar IN-SSD-DAM    2026-03-25 2026-05-10

echo
"${TS[@]}" verify
