#!/usr/bin/env python3
"""Print to an Epson TM-T88V over USB.

Examples:
  ./receipt "hello world"
  echo "from stdin" | ./receipt
  ./receipt --bold --size 3 "BIGGER TEXT"
  ./receipt --left --size 1 "small, left-aligned"
  ./receipt --qr https://hackclub.com
  ./receipt --image cat.png
  ./receipt --no-cut "keep going..."
"""
import argparse
import sys
import textwrap

import usb.core
from escpos.printer import Usb

EPSON_VENDOR_ID = 0x04B8

# Our print head has dead heating elements at dots ~183-192 (found with a
# ruler test print); text is laid out so no character lands on them.
# Dot 192 is left out on purpose: it's the first dot of a character cell,
# which is spacing in Font A, so the gap is one character wide, not two.
DEAD_DOTS = (183, 191)
PAPER_DOTS = 512
FONT_A_WIDTH = 12  # dots per character at size 1

# fancy punctuation the printer's code pages may not have
PLAIN = str.maketrans({"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "..."})
# control characters (ESC, GS, DLE, ...) are printer commands: calendar invites
# and to-do titles must not be able to send them, so they're dropped
PLAIN.update({c: None for c in [*range(0x20), *range(0x7F, 0xA0)]})


def blocked_columns(size):
    """Character columns that overlap the dead dots at this text size."""
    cw = FONT_A_WIDTH * size
    first, last = DEAD_DOTS
    return set(range(first // cw, last // cw + 1))


def place_words(words, start, ncols, blocked):
    """Place words left to right from `start`, hopping over blocked columns.

    Returns (placements, rest): [(col, word), ...] for the words that fit
    on this line, and the words that didn't.
    """
    placed, col = [], start
    for i, word in enumerate(words):
        if placed:
            col += 1  # space between words
        while col < ncols and col in blocked:
            col += 1
        cols = range(col, col + len(word))
        if any(c in blocked for c in cols):
            if len(word) <= ncols - max(blocked) - 1:
                # it fits after the gap: jump the whole word past it
                col = max(blocked) + 1
                cols = range(col, col + len(word))
            else:
                # too long for either side: split letters around the gap
                cols, c = [], col
                while len(cols) < len(word):
                    if c not in blocked:
                        cols.append(c)
                    c += 1
        if cols[-1] >= ncols:
            if placed:
                return placed, words[i:]
            # a single word wider than the whole line: hard-wrap it
            fit = sum(1 for c in range(col, ncols) if c not in blocked)
            if fit == 0:
                return placed, words[i:]
            head, tail = word[:fit], word[fit:]
            placed += place_words([head], col, ncols, blocked)[0]
            return placed, [tail] + words[i + 1:]
        placed.append((cols, word))
        col = cols[-1] + 1
    return placed, []


def render(placed, ncols):
    line = [" "] * ncols
    for cols, word in placed:
        for c, ch in zip(cols, word):
            line[c] = ch
    return "".join(line).rstrip().replace("\u00a0", " ")


def layout(text, size, center):
    """Turn text into printer lines that avoid the dead columns.

    Returns (lines, wrapped). Centering is only used if nothing wraps.
    """
    ncols = PAPER_DOTS // (FONT_A_WIDTH * size)
    blocked = blocked_columns(size)
    # split on plain spaces only, so a non-breaking space keeps words together
    paragraphs = [[w for w in para.split(" ") if w] for para in text.splitlines()]

    wrapped = False
    for words in paragraphs:
        if words and place_words(words, 0, ncols, blocked)[1]:
            wrapped = True

    out = []
    for words in paragraphs:
        if not words:
            out.append("")
            continue
        if center and not wrapped:
            # try every start column, keep the one closest to the middle
            best = None
            for start in range(ncols):
                placed, rest = place_words(words, start, ncols, blocked)
                if rest:
                    break
                mid = (placed[0][0][0] + placed[-1][0][-1]) / 2
                score = abs(mid - (ncols - 1) / 2)
                if best is None or score < best[0]:
                    best = (score, placed)
            out.append(render(best[1], ncols))
            continue
        while words:
            placed, words = place_words(words, 0, ncols, blocked)
            out.append(render(placed, ncols))
    return out, wrapped


def find_printer():
    dev = usb.core.find(idVendor=EPSON_VENDOR_ID)
    if dev is None:
        sys.exit("No Epson printer found on USB. Is it plugged in and turned on?")
    # profile sets the paper width / capabilities for image scaling etc.
    return Usb(EPSON_VENDOR_ID, dev.idProduct, profile="TM-T88V")


def two_columns(rows, size=1):
    """Lay out (label, value) rows with the dead dots as the column gutter.

    Labels sit left of the gap, values wrap on the right side of it.
    """
    ncols = PAPER_DOTS // (FONT_A_WIDTH * size)
    blocked = blocked_columns(size)
    left_width, right_start = min(blocked), max(blocked) + 1
    out = []
    for label, value in rows:
        # textwrap won't break at a non-breaking space, so "09:00\u00a014°" stays together
        wrapped = textwrap.wrap(value, ncols - right_start) or [""]
        for i, chunk in enumerate(wrapped):
            left = label[:left_width] if i == 0 else ""
            out.append(f"{left:<{right_start}}{chunk}".rstrip().replace("\u00a0", " "))
    return out


def flow_rows(rows, size=1):
    """Lay out (label, value) rows across the full width, hopping the dead dots.

    The label starts each row; wrapped lines are indented under the value.
    """
    ncols = PAPER_DOTS // (FONT_A_WIDTH * size)
    blocked = blocked_columns(size)
    out = []
    for label, value in rows:
        label_words = [w for w in label.split(" ") if w]
        words = label_words + [w for w in value.split(" ") if w]
        if not words:
            out.append("")
            continue
        placed, rest = place_words(words, 0, ncols, blocked)
        out.append(render(placed, ncols))
        indent = len(label) + 1 if label and len(label) < ncols // 3 else 2
        while rest:
            placed, rest = place_words(rest, indent, ncols, blocked)
            out.append(render(placed, ncols))
    return out


def _move_to(printer, dot):
    """ESC $: put the next character at this dot from the start of the line."""
    printer._raw(b"\x1b$" + dot.to_bytes(2, "little"))


def _emit(printer, lines, size):
    """Send laid-out lines, squeezing the blank column down to just the dead dots.

    layout() leaves a whole character column blank over the dead dots. Here the
    text left of it is nudged right to end where the dead dots start, and the
    text right of it starts where they end, so the gap is no wider than it must be.
    """
    blocked = blocked_columns(size)
    first, last = min(blocked), max(blocked)
    nudge = DEAD_DOTS[0] - first * FONT_A_WIDTH * size
    for line in lines:
        line = line.translate(PLAIN)
        left, right = line[:first], line[last + 1:]
        if left.strip():
            _move_to(printer, nudge)
            printer.text(left)
        if right.strip():
            _move_to(printer, DEAD_DOTS[1] + 1)
            printer.text(right)
        printer.text("\n")


def print_lines(printer, lines, size=1, bold=False):
    """Print lines exactly as given (already laid out around the dead dots)."""
    printer.set(align="left", bold=bold, custom_size=size > 1, width=size, height=size)
    _emit(printer, lines, size)
    printer.set_with_default()


def print_text(printer, text, size=2, center=True, bold=False, underline=False):
    """Print text laid out around the dead dots (centered unless it wraps)."""
    lines, _ = layout(text, size, center)
    printer.set(
        align="left",  # layout() already positioned everything
        bold=bold,
        underline=1 if underline else 0,
        custom_size=size > 1,
        width=size,
        height=size,
    )
    _emit(printer, lines, size)
    printer.set_with_default()


def main():
    p = argparse.ArgumentParser(description="Print to a TM-T88V receipt printer.")
    p.add_argument("text", nargs="*", help="text to print (reads stdin if omitted)")
    p.add_argument("--image", help="path to an image to print")
    p.add_argument("--qr", help="print a QR code for this data")
    p.add_argument("--bold", action="store_true")
    p.add_argument("--underline", action="store_true")
    p.add_argument("--left", action="store_true", help="left-align (default: centered unless the text wraps)")
    p.add_argument("--size", type=int, choices=range(1, 9), default=2,
                   help="text magnification 1-8")
    p.add_argument("--no-cut", action="store_true", help="don't cut the paper")
    args = p.parse_args()

    text = " ".join(args.text)
    if not text and not args.image and not args.qr and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not (text or args.image or args.qr):
        p.error("nothing to print")

    printer = find_printer()
    try:
        if text:
            print_text(printer, text, args.size, center=not args.left,
                       bold=args.bold, underline=args.underline)
        if args.image:
            printer.image(args.image, center=not args.left)
        if args.qr:
            printer.qr(args.qr, size=8, center=not args.left)
        printer.set_with_default()
        if args.no_cut:
            printer.ln(2)
        else:
            printer.cut()
    finally:
        printer.close()


if __name__ == "__main__":
    main()
