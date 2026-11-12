#!/usr/bin/env python3
"""sheet.py: contact sheet of frames from clips.  usage: sheet.py out.png clip@t [clip@t ...]  (2x2 of 960x540 per sheet)"""
import subprocess, sys, os, tempfile
from PIL import Image
out, items = sys.argv[1], sys.argv[2:]
tmp = tempfile.mkdtemp()
ims = []
for i, it in enumerate(items):
    clip, t = it.rsplit('@', 1)
    f = os.path.join(tmp, f'{i}.png')
    subprocess.run(['./rec/vtool', 'frame', clip, t, f, '960'], capture_output=True)
    ims.append(Image.open(f).convert('RGB').resize((960, 540)))
cols = 2; rows = (len(ims) + 1) // 2
sh = Image.new('RGB', (1920, 540 * rows), 'black')
for i, im in enumerate(ims):
    sh.paste(im, ((i % 2) * 960, (i // 2) * 540))
sh.save(out)
