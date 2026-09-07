#!/usr/bin/env python3
"""Regenerate the static field manual from canonical Markdown (requires Pandoc)."""
import hashlib
import html
import os
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SITE=ROOT/'docs/site'


def sources():
    return {ROOT/'README.md':SITE/'index.html', **{p:SITE/(p.stem.lower()+'.html') for p in (ROOT/'docs').glob('*.md') if p.name != 'CODEX-SUPPORT-PLAN.md'}}


def main():
    pages=sources()
    for source, target in pages.items():
        rendered=subprocess.check_output(['pandoc',str(source),'--from=gfm','--to=html5','--standalone',
            '--template',str(ROOT/'scripts/docs-page.html'),'--metadata','pagetitle='+source.stem,
            '--metadata','sourcehash='+hashlib.sha256(source.read_bytes()).hexdigest()],text=True)
        def link(match):
            attribute, raw=match.groups(); value=html.unescape(raw)
            if value.startswith(('#','/')) or re.match(r'^[a-z]+:',value): return match.group(0)
            path, sep, fragment=value.partition('#')
            resolved=(source.parent/path).resolve()
            # Template navigation/assets are already site-relative.
            if value in ('assets/colors_and_type.css','assets/docs.css') or (path.endswith('.html') and '/' not in path): return match.group(0)
            destination=pages.get(resolved,resolved)
            relative=os.path.relpath(destination,target.parent)+(sep+fragment if sep else '')
            return attribute+'="'+html.escape(relative,quote=True)+'"'
        rendered=re.sub(r'(href|src)="([^"]+)"',link,rendered)
        target.write_text(rendered)
        print(target.relative_to(ROOT))


if __name__=='__main__': main()
