"""Verify a downloaded JSON array of readiness snapshots offline, without DB access."""
import argparse
import json
from trader.operations.readiness import evidence_digest


def verify(entries):
    chron = sorted(entries,key=lambda x:x['id'])
    prev = None
    for row in chron:
        if row['previous_sha256'] != prev:
            raise ValueError(f'Invalid previous digest at snapshot #{row["id"]}')
        if evidence_digest(row['report'],prev) != row['sha256']:
            raise ValueError(f'Invalid digest at snapshot #{row["id"]}')
        prev = row['sha256']
    return len(chron)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('json_path')
    args=p.parse_args()
    with open(args.json_path,encoding='utf-8') as f:
        rows=json.load(f)
    print(f'Verified {verify(rows)} hash-linked snapshots')
