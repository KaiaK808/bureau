#!/bin/bash
# Exercise the installed app tests action with a local venv dependency and HTTP endpoint.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
python3 -m venv --without-pip .venv
SITE=$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
printf 'ANSWER = "local dependency works"\n' > "$SITE/bureau_fixture_dependency.py"
cat > probe.py <<'PY'
import http.server, os, socket, threading, urllib.request
from bureau_fixture_dependency import ANSWER
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(ANSWER.encode())
    def log_message(self,*args): pass
try: server=http.server.HTTPServer(('127.0.0.1',0),Handler)
except PermissionError:
    if os.environ.get('BUREAU_REQUIRE_LOCAL_SERVER') == '1': raise
    print('SKIP local server: environment denies loopback binding');raise SystemExit(0)
worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
try:
    with urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port),timeout=3) as response:
        assert response.status == 200 and response.read().decode() == ANSWER
    print('PASS local venv dependency and HTTP request through app test action')
finally: server.shutdown();server.server_close();worker.join(timeout=3)
PY
printf '{"repo":{"test_command":".venv/bin/python probe.py"}}\n' > .bureau.json
BUREAU_CONFIG="$TMP/.bureau.json" bash "$ROOT/templates/scripts/bureau-app.sh" test
