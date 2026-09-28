#!/bin/bash
# curl-writeout.sh <http-status> <curl-args...> — prints what real curl prints
# after the body for `-w FORMAT`: FORMAT with `\n` expanded and %{http_code}
# filled in. Curl stubs call it as their last output, so the Linear fetch
# (templates/scripts/bureau-config.sh, _bureau_linear_fetch) finds the status
# line it reads from real curl. Without it the fetch does not judge the status
# at all, so a test of the status check must print one.
status="$1"; shift
prev=""; fmt=""
for a in "$@"; do [ "$prev" = "-w" ] && fmt="$a"; prev="$a"; done
[ -n "$fmt" ] || exit 0
fmt="${fmt/\%\{http_code\}/$status}"
printf '%b' "$fmt"
