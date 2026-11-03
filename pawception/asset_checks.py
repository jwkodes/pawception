"""Verify bundled demo media against its committed size and SHA-256 records."""

import argparse
import json

from pawception.paths import ROOT
from pawception.video import ffmpeg
from pawception.assets import verify_file


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--decode',action='store_true',help='Also fully decode every video and its available audio')
    args=parser.parse_args()
    data=ROOT/'data';count=0
    for name in ('online_manifest.json',):
        for clip in json.loads((data/'demo'/name).read_text())['clips']:
            entry=clip['working'];path=data/entry['file']
            verify_file(path,entry)
            if args.decode:ffmpeg('-i',path,'-map','0:v:0','-map','0:a?','-f','null','-')
            print(f"Verified: {clip['id']} ({entry['bytes']/1e6:.1f} MB)")
            count+=1
    print(f'{count} demo clips verified. No downloads or API calls.')
