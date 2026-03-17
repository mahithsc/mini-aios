#!/usr/bin/env bash
set -euo pipefail

mkdir -p data

# Fetch latest NVDA quote from Stooq (free delayed data). Output is CSV.
# Fields: Symbol,Date,Time,Open,High,Low,Close,Volume
url="https://stooq.com/q/l/?s=nvda.us&f=sd2t2ohlcv&h&e=csv"

ts="$(date +%F_%H%M%S)"
out="data/nvda_quote_${ts}.csv"

curl -fsSL "$url" > "$out"

echo "Wrote: $out"
head -n 2 "$out" | sed 's/^/  /'