#!/usr/bin/env python3
"""Add a BetterTTV emote to the addon.

    python tools/add_emote.py https://betterttv.com/emotes/<id> PepeCool
    python tools/add_emote.py <id> PepeCool --dir Pepes --pack Pepe

Downloads the emote, converts it to a texture the 1.12 client can decode, and
registers it in Emotes.lua (and in the minimap dropdown's pack list).

Two client constraints drive the conversion:

  * Textures are capped at 1024px per side and power-of-two, so an animation
    longer than 32 frames is packed row-major across several 32px columns
    rather than one over-tall strip. TwitchEmotesAnimator derives the column
    count from imageWidth / frameWidth.
  * The animator plays frames at one constant rate, but GIFs hold individual
    frames for different lengths. Uneven delays are expanded into repeated
    frames at whichever integer rate reproduces the original timing most
    closely within the frame budget.
"""
import argparse
import os
import re
import struct
import sys
import urllib.request

from PIL import Image, ImageSequence

CDN = 'https://cdn.betterttv.net/emote/%s/%s'
FRAME = 32               # frame size in the sheet; emotes render at 28x28
MAX_TEXTURE = 1024       # client cap, per side
MAX_COLS = 4             # 4 * 32 = 128px wide, 128 frames at most
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def emote_id(arg):
    m = re.search(r'([0-9a-f]{24})', arg)
    if not m:
        sys.exit('not a BetterTTV emote id or URL: %s' % arg)
    return m.group(1)


def download(eid):
    last = None
    for size in ('3x', '2x', '1x'):
        try:
            with urllib.request.urlopen(CDN % (eid, size), timeout=30) as r:
                return r.read()
        except Exception as exc:            # noqa: BLE001 - try the next size
            last = exc
    sys.exit('could not download emote %s: %s' % (eid, last))


def load_frames(blob):
    """Frames as 32x32 RGBA, plus each frame's display duration in ms."""
    from io import BytesIO
    im = Image.open(BytesIO(blob))
    frames, durations = [], []
    for page in ImageSequence.Iterator(im):
        rgba = page.convert('RGBA')
        fitted = Image.new('RGBA', (FRAME, FRAME), (0, 0, 0, 0))
        scaled = rgba.copy()
        scaled.thumbnail((FRAME, FRAME), Image.LANCZOS)
        fitted.paste(scaled, ((FRAME - scaled.width) // 2,
                              (FRAME - scaled.height) // 2))
        frames.append(fitted)
        durations.append(page.info.get('duration') or 0)
    return frames, durations


TIMING_TOLERANCE = 0.05  # 5% drift over a loop is not perceptible


def pick_framerate(durations, budget):
    """Integer fps whose repeated frames reproduce the source timing.

    Returns (fps, repeats). Every frame is shown at least once, so a rate too
    slow to represent the shortest delay simply rounds it up. Among the rates
    that hold the loop length within tolerance, the one needing the fewest
    frames wins - a faster rate can always match the duration more exactly, but
    it pays for that in texture size.
    """
    total = sum(durations) / 1000.0
    candidates = []
    for fps in range(1, 31):
        repeats = [max(1, int(round(d * fps / 1000.0))) for d in durations]
        if sum(repeats) > budget:
            continue
        error = abs(sum(repeats) / float(fps) - total)
        candidates.append((error, sum(repeats), fps, repeats))
    if not candidates:
        sys.exit('animation does not fit in %d frames' % budget)
    tolerance = max(total * TIMING_TOLERANCE, 0.05)
    within = [c for c in candidates if c[0] <= tolerance]
    best = min(within or candidates, key=lambda c: (c[1], c[0]))
    return best[2], best[3]


def layout(count):
    """Smallest column count whose rows fit the texture cap."""
    for cols in (1, 2, 4):
        rows = -(-count // cols)
        if rows * FRAME <= MAX_TEXTURE:
            height = FRAME
            while height < rows * FRAME:
                height *= 2
            return cols, height
    sys.exit('%d frames exceed the %dpx texture cap' % (count, MAX_TEXTURE))


def write_tga(im, path):
    """Uncompressed 32-bit BGRA, top-down origin - what the client decodes."""
    w, h = im.size
    r, g, b, a = im.split()
    header = struct.pack('<BBBHHBHHHHBB', 0, 0, 2, 0, 0, 0, 0, 0, w, h, 32, 0x28)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(Image.merge('RGBA', (b, g, r, a)).tobytes())


def build_sheet(frames, repeats):
    expanded = []
    for frame, n in zip(frames, repeats):
        expanded.extend([frame] * n)
    cols, height = layout(len(expanded))
    sheet = Image.new('RGBA', (cols * FRAME, height), (0, 0, 0, 0))
    for i, frame in enumerate(expanded):
        sheet.paste(frame, ((i % cols) * FRAME, (i // cols) * FRAME))
    return sheet, len(expanded), cols, height


def insert_before(text, anchor, line):
    at = text.rindex(anchor)
    return text[:at] + line + '\n' + text[at:]


def register(name, texdir, pack, sheet_info, replace=False):
    """Add the emote to defaultpack, emoticons, the animation table and the menu."""
    path = 'Interface\\\\AddOns\\\\TwitchEmotes\\\\Emotes\\\\%s\\\\%s.tga' % (texdir, name)
    emotes_lua = os.path.join(ROOT, 'Emotes.lua')
    src = open(emotes_lua, encoding='utf-8').read()
    known = '["%s"]=' % name in src or '["%s"] =' % name in src
    if known and not replace:
        sys.exit('%s is already registered in Emotes.lua (pass --replace to '
                 'update it in place)' % name)

    if sheet_info:
        nframes, cols, height, fps = sheet_info
        spec = '%s:28:28:0:0:%d:%d:0:%d:0:%d' % (path, cols * FRAME, height, FRAME, FRAME)
    else:
        spec = '%s:28:28' % path

    if known:
        # Only the texture spec changes; the emoticons entry and the menu
        # already name this emote. Match the defaultpack row by its value, so
        # the emoticons row (name -> name) is left alone.
        src, hits = re.subn(r'(\["%s"\]\s*=\s*")Interface\\\\AddOns[^"]*(")'
                            % re.escape(name), lambda m: m.group(1) + spec + m.group(2),
                            src, count=1)
        if hits != 1:
            sys.exit('could not find the defaultpack row for %s' % name)
    else:
        # defaultpack (name -> texture) and emoticons (typed token -> name) are
        # two separate tables; Emoticons_Deformat walks emoticons and indexes
        # defaultpack with the same key, so both need the name.
        src = insert_before(src, '\n  };\n  emoticons={',
                            '\t["%s"]="%s",' % (name, spec))
        src = insert_before(src, '\n  };\n\nTwitchEmotes_animation_metadata',
                            '\t["%s"]="%s",' % (name, name))

    # A sheet needs an animation entry; a static texture must not have one.
    src = re.sub(r'\t\["%s"\] = \{\["nFrames"\][^\n]*\n' % re.escape(path), '', src)
    if sheet_info:
        entry = ('\t["%s"] = {["nFrames"] = %d, ["frameWidth"] = %d, '
                 '["frameHeight"] = %d, ["imageWidth"]=%d, ["imageHeight"]=%d, '
                 '["framerate"] = %d},' %
                 (path, nframes, FRAME, FRAME, cols * FRAME, height, fps))
        src = src.rstrip('\n')
        assert src.endswith('}'), 'unexpected end of Emotes.lua'
        src = src[:-1] + entry + '\n}\n'
    open(emotes_lua, 'w', encoding='utf-8', newline='').write(src)
    if known:
        return

    # dropdown_options in TwitchEmotes.lua drives the minimap menu; keep each
    # pack sorted, since the submenu pages are labelled by their first and last
    # entry.
    main_lua = os.path.join(ROOT, 'TwitchEmotes.lua')
    main = open(main_lua, encoding='utf-8').read()
    pat = re.compile(r'(\[\d+\]=\s*\{"%s"(.*?)\},\n)' % re.escape(pack))
    m = pat.search(main)
    if not m:
        sys.exit('no dropdown pack named %r in TwitchEmotes.lua' % pack)
    names = re.findall(r'"([^"]*)"', m.group(1))
    if name in names[1:]:
        sys.exit('%s is already in the %s menu' % (name, pack))
    body = sorted(names[1:] + [name], key=lambda s: s.lower())
    # the match starts at '[' so the line's existing indent is already in place
    line = '[%s]=  {%s},\n' % (
        re.match(r'\[(\d+)\]', m.group(1)).group(1),
        ','.join('"%s"' % n for n in [names[0]] + body))
    main = main[:m.start(1)] + line + main[m.end(1):]
    open(main_lua, 'w', encoding='utf-8', newline='').write(main)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('emote', help='BetterTTV emote id or URL')
    ap.add_argument('name', help='chat trigger word, e.g. PepeCool')
    ap.add_argument('--dir', default='Custom', help='folder under Emotes/ (default: Custom)')
    ap.add_argument('--pack', default='Custom', help='minimap menu pack (default: Custom)')
    ap.add_argument('--replace', action='store_true',
                    help='update an emote that is already registered')
    ap.add_argument('--dry-run', action='store_true', help='report only, write nothing')
    args = ap.parse_args()

    if not re.match(r'^[%:\w]+$', args.name):
        sys.exit('name must be a plain word or :token: - got %r' % args.name)

    frames, durations = load_frames(download(emote_id(args.emote)))
    tga = os.path.join(ROOT, 'Emotes', args.dir, args.name + '.tga')

    if len(frames) == 1:
        sheet, info = frames[0], None
        print('%s: static 32x32' % args.name)
    else:
        fps, repeats = pick_framerate(durations, MAX_COLS * (MAX_TEXTURE // FRAME))
        sheet, nframes, cols, height = build_sheet(frames, repeats)
        info = (nframes, cols, height, fps)
        print('%s: %d source frames (%.2fs) -> %d frames at %dfps (%.2fs), '
              '%dx%d sheet in %d column(s)' %
              (args.name, len(frames), sum(durations) / 1000.0, nframes, fps,
               nframes / float(fps), cols * FRAME, height, cols))

    if args.dry_run:
        print('dry run: would write %s' % os.path.relpath(tga, ROOT))
        return
    write_tga(sheet, tga)
    register(args.name, args.dir, args.pack, info, args.replace)
    print('wrote %s and %s' % (os.path.relpath(tga, ROOT),
                               'updated its registration' if args.replace
                               else 'registered it in the %s pack' % args.pack))


if __name__ == '__main__':
    main()
