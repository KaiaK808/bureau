#!/bin/bash
# Probes for tests/test_env_keys_unexported.sh (v3.2: the stages keep the .env keys
# unexported). Source after tests/lib/harness.sh or tests/lib/pr5-interrupt.sh.
#
#   c1_probe_tools <bin dir> <log> [tool ...]
#       Puts a probe in <bin dir> for each tool (default: jq python3 date mktemp cat sed grep
#       head tail tr wc sort cut mkdir rm basename dirname tee). A probe is a /bin/sh script
#       that appends one line to <log> — "<tool> <names>", where <names> is the
#       space-separated list of environment variables whose value carries one of the three
#       .env keys (or whose name is one of them), or "clean" — and then execs the real tool
#       found on PATH when the probe was written. A probe records only once the process
#       carries the marker C1_MARK_NAME=C1_MARK_VALUE, i.e. after the script that started it
#       has loaded its .env (the marker is a non-secret .env key, which --export still
#       exports), so the lines before the load do not count.
#   c1_check_log <log> <label>
#       At least one probe line, and no line that names a variable.
# Failures go through c1_fail, which counts them in C1_FAILS.

C1_LINEAR=lin_api_PROBE_c1_linear_key_000000000000000000000001
C1_TG_TOKEN=123456789:PROBE_c1_telegram_bot_token_00000002
C1_TG_CHAT=-1001234567890
C1_MARK_NAME=BUREAU_PATH_PREFIX_STRIP
C1_MARK_VALUE=c1-env-loaded/
C1_FAILS=${C1_FAILS:-0}

c1_fail() { echo "FAIL $*" >&2; C1_FAILS=$((C1_FAILS + 1)); }

# c1_env_file <file> [extra line ...] — the three keys with long probe values and the marker.
c1_env_file() {
  local file="$1"; shift
  {
    printf 'LINEAR_API_KEY=%s\nTELEGRAM_BOT_TOKEN=%s\nTELEGRAM_ALERT_CHAT_ID=%s\n%s=%s\n' \
      "$C1_LINEAR" "$C1_TG_TOKEN" "$C1_TG_CHAT" "$C1_MARK_NAME" "$C1_MARK_VALUE"
    [ "$#" = 0 ] || printf '%s\n' "$@"
  } > "$file"
}

c1_probe_tools() {
  local dir="$1" log="$2" tool real
  shift 2
  [ "$#" -gt 0 ] || set -- jq python3 date mktemp cat sed grep head tail tr wc sort cut mkdir rm basename dirname tee
  mkdir -p "$dir"
  for tool in "$@"; do
    real=$(PATH="${PATH#"$dir:"}" command -v "$tool") || continue
    case "$real" in "$dir"/*) continue ;; esac
    cat > "$dir/$tool" <<EOF
#!/bin/sh
# The options the shell that started the probe passed on (SHELLOPTS; empty under dash, which leaves
# it alone): with xtrace or allexport in them (test_env_keys_*.sh D and 5) the tool gets them back
# unchanged, so a traced stage stays traced past a probed python3 or jq, minus the POSIX mode a
# /bin/sh that is bash adds. The probe itself does not trace (its constants are the key values).
c1_opts=\${SHELLOPTS:-}
{ set +x; } 2>/dev/null
if [ "\${$C1_MARK_NAME:-}" = '$C1_MARK_VALUE' ]; then
  hit=\$(/usr/bin/env | /usr/bin/awk -F= -v a='$C1_LINEAR' -v b='$C1_TG_TOKEN' -v c='$C1_TG_CHAT' \
    '\$1 == "LINEAR_API_KEY" || \$1 == "API_KEY" || \$1 == "TELEGRAM_BOT_TOKEN" || \$1 == "TELEGRAM_ALERT_CHAT_ID" || index(\$0, a) || index(\$0, b) || index(\$0, c) { printf "%s ", \$1 }')
  printf '%s %s\n' '$tool' "\${hit:-clean}" >> '$log'
fi
case ":\$c1_opts:" in *:xtrace:*|*:allexport:*)
  exec /usr/bin/env SHELLOPTS="\$(printf '%s' "\$c1_opts" | /usr/bin/sed -e 's/:posix:/:/' -e 's/^posix://' -e 's/:posix\$//')" '$real' "\$@" ;;
esac
exec '$real' "\$@"
EOF
    chmod +x "$dir/$tool"
  done
}

c1_check_log() {
  local log="$1" label="$2"
  if [ ! -s "$log" ]; then c1_fail "$label: no process started after the .env load was recorded"; return; fi
  if grep -v ' clean$' "$log" >/dev/null; then
    c1_fail "$label: a process the script started carries a .env key: $(grep -v ' clean$' "$log" | sort | uniq -c | sed -n 1,5p | tr -s ' ' | tr '\n' ';')"
  fi
}
