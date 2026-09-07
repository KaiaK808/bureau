#!/usr/bin/env python3
"""Compare bounded-tick outcomes; repeated unchanged results stay quiet."""
import argparse
import json
import uuid
from pathlib import Path


def compare(result, previous):
    keys=('outcome','issue','stage','before','after','exit_code')
    current={k:result.get(k) for k in keys}
    prior={k:previous.get(k) for k in keys} if previous else None
    changed=current != prior
    actionable=result.get('outcome') in ('advanced','completed','failed','blocked','stopped_for_review')
    return dict(changed=changed,notify=changed and actionable,result=current)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result'); parser.add_argument('--previous',required=True)
    args=parser.parse_args()
    path=Path(args.previous); result=json.loads(Path(args.result).read_text())
    previous=json.loads(path.read_text()) if path.exists() else None
    output=compare(result,previous)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name + '.' + uuid.uuid4().hex); temp.write_text(json.dumps(output['result'])+'\n'); temp.replace(path)
    print(json.dumps(output))


if __name__=='__main__': main()
