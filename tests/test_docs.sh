#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import hashlib, html, pathlib, re, sys
root=pathlib.Path(sys.argv[1]);site=root/'docs/site'
pages={root/'README.md':site/'index.html',**{p:site/(p.stem.lower()+'.html') for p in (root/'docs').glob('*.md') if p.name!='CODEX-SUPPORT-PLAN.md'}}
for source,target in pages.items():
    content=target.read_text()
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    assert f'name="bureau-source-sha256" content="{digest}"' in content, f'Render docs after editing {source}'
    for raw in re.findall(r'(?:href|src)="([^"]+)"',content):
        link=html.unescape(raw)
        if link.startswith(('#','/')) or re.match(r'^[a-z]+:',link):continue
        assert (target.parent/link.split('#')[0]).exists(),(target,link)
assert '<td><code>25</code></td>' in (site/'exit-codes.html').read_text()
print('PASS rendered docs match canonical sources and local links resolve')
PY
