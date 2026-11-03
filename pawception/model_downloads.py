"""Download or verify optional, pinned local weights; no model inference."""

import argparse
import json

from pawception.paths import ROOT
from pawception.assets import prepare_file


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model',choices=('moge',))
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args()
    sources=json.loads((ROOT/'data/model_sources.json').read_text())
    for record in sources[args.model]:
        print(f"{record['file']}: {prepare_file(record,ROOT/'data',args.verify_only)}",flush=True)
