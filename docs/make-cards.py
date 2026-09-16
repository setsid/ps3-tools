#!/usr/bin/env python3
"""
Draws the eight title cards README.md puts above everything else.

One card a game, each a link to that title's notes. They are generated rather
than drawn by hand for two reasons. The first is that they carry status, which
changes: a title moves from untested to investigating to fixed as the work
happens, and a status that has to be edited in eight hand written SVGs is a
status that goes stale. The second is colour. The cards use the application's
own palette, taken from ps3tools/shell/theme.py at the top of this file, so a
change to the theme is a re-run here rather than a hunt through hex values.

Nothing here is anyone else's artwork. Every card is a rectangle, a rounded
square and three pieces of text, all drawn from these shapes and these
letters, because the titles themselves are trademarks and their logos and key
art are not ours to ship.

    python3 docs/make-cards.py

The output is deterministic. Re-running with no source change rewrites the same
bytes, so the cards never turn up as noise in a diff. The run also prints the
widest title against the room a card has for it and the contrast of every
status colour, because a title that spills and a word nobody can read are the
two ways this file goes wrong, and both were found by looking rather than by
reading the code.

The cards have to survive GitHub serving one file to a light page and a dark
one. GitHub strips <style> out of SVGs in a repository, so prefers-colour-
scheme is not available and a card cannot ask the page which way round it is.
Instead each card paints its own opaque plate edge to edge and draws on that,
which means the page behind it never shows through and never has a say.

The card is laid out down the page rather than across: tile, then title, then
status, all centred. Beside the tile there was only room for about half of
"Modern Warfare 3", and an SVG cannot reflow, so the long titles ran off the
right hand edge. Stacking gives the title the whole width and lets the card be
180 across instead of 220, and four of those fit a README's content column
where four of the old ones did not.
"""

import os

# -- the palette -------------------------------------------------------------

#: PALETTES["dark"] from ps3tools/shell/theme.py, copied rather than imported
#: because importing it drags PySide6 in, and a documentation script should run
#: on a machine that has nothing installed. Only the tokens the cards draw with
#: are here. If the theme moves, move these with it.
DARK = {
    "bg": "#0d0f14",
    "surface": "#161a22",
    "border": "#2d3543",
    "text": "#e9ecf3",
    "text_dim": "#9aa4b6",
    "ok": "#4fd39a",
    "info": "#79b8ff",
}

#: Which token each status word is drawn in. The two live states are the two
#: bright colours so they carry across the grid at a glance, and the two that
#: are not being worked on fall back to the quiet text colour.
#:
#: Out of scope was drawn in border until it was looked at on a screen. border
#: is a hairline colour and not a text colour: it came out at 1.41:1 on the
#: card, which is a word nobody can read rather than a word deliberately made
#: quiet. It is text_dim now, like untested, and the difference between the
#: two is drawn instead of coloured. See DASHED.
STATUS_TOKENS = {
    "fixed": "ok",
    "investigating": "info",
    "untested": "text_dim",
    "out of scope": "text_dim",
}

#: Statuses whose card is drawn with a broken edge rather than a solid one.
#: Untested and out of scope are both quiet, and one of them is quiet because
#: nobody has got to it while the other is quiet because there is nothing to
#: get to. A dashed edge is how the application itself draws a card it will
#: not act on, so the difference is one somebody can see and one that already
#: means this elsewhere in the program.
DASHED = {"out of scope"}

# -- the cards ---------------------------------------------------------------

#: (file name, tile letters, title, status). The order is the order of the grid
#: in README.md, which is worst affected and best understood first.
CARDS = (
    ("black-ops-2.svg", "B2", "Black Ops II", "fixed"),
    ("modern-warfare-3.svg", "M3", "Modern Warfare 3", "fixed"),
    ("black-ops.svg", "B1", "Black Ops", "fixed"),
    ("modern-warfare-2.svg", "M2", "Modern Warfare 2", "investigating"),
    ("world-at-war.svg", "WW", "World at War", "untested"),
    ("modern-warfare.svg", "MW", "Modern Warfare", "untested"),
    ("ghosts.svg", "GH", "Ghosts", "untested"),
    ("advanced-warfare.svg", "AW", "Advanced Warfare", "out of scope"),
)

# -- geometry ----------------------------------------------------------------

WIDTH = 180
HEIGHT = 130

#: The plate is the full canvas and the card is inset inside it, so the two
#: together read as a panel in the application rather than as a rectangle that
#: has lost its background. Half pixel offsets keep the one pixel edge on the
#: pixel grid instead of straddling two.
INSET = 6.5
PLATE_RADIUS = 14
CARD_RADIUS = 10

CENTRE_X = WIDTH / 2

#: The tile is centred near the top, with the two lines of text stacked under
#: it. Everything on the card shares one centre line.
TILE_SIDE = 44
TILE_X = (WIDTH - TILE_SIDE) / 2
TILE_Y = 16
TILE_RADIUS = 10

#: Two capitals in bold Verdana at 17px come to about 38 at worst, which is
#: "WW", and the tile is 44 across, so the widest pair still has air beside it.
TILE_SIZE = 17

#: The tile letters are centred on the tile. SVG positions text by its
#: baseline, and about a third of the size below the centre is where a run of
#: capitals looks centred to the eye.
TILE_BASELINE = TILE_Y + TILE_SIDE / 2 + TILE_SIZE * 0.35

TITLE_Y = 88
TITLE_SIZE = 13
STATUS_Y = 110
STATUS_SIZE = 10.5

#: What a centred line of text may use. The card is 167 wide inside its edge
#: and a line wants a margin either side of it rather than running up to the
#: stroke. Any title wider than this is a title that spills.
TEXT_LIMIT = WIDTH - INSET * 2 - 16

#: A plain stack, because a card is rendered by whatever the reader's browser
#: has rather than by us. Every size is stated outright for the same reason.
FONT = "DejaVu Sans, Verdana, sans-serif"

#: Advance per character as a fraction of the font size, for the estimate
#: below. Taken from the two fonts that actually get used: mixed case in bold
#: averages a little under 0.65 of the size a character in both DejaVu Sans
#: Bold and Verdana Bold, and less than that in regular. It is meant to be
#: pessimistic, because the cost of being wrong is a title hanging off the
#: side of something that cannot reflow.
BOLD_ADVANCE = 0.65
REGULAR_ADVANCE = 0.60


def estimate_width(text, size, bold=False):
    """Roughly how wide this text will be drawn, in pixels.

    Deliberately crude. Which fonts a reader has is not knowable from here, so
    what this feeds is a guard against a title spilling out of the card, not a
    typesetting engine.
    """
    advance = BOLD_ADVANCE if bold else REGULAR_ADVANCE
    return len(text) * size * advance


def escape(text):
    """The five characters XML will not take raw. No title has any of them
    today, but a title added later should not silently produce broken markup.
    """
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&apos;"))


def number(value):
    """Trim a trailing ".0" so whole numbers are written as whole numbers.

    Only cosmetic, but it keeps the generated markup readable for anyone who
    opens a card to check what it actually is.
    """
    return f"{value:g}"


def card_svg(letters, title, status):
    """One card, as a complete SVG document ending in a newline."""
    accent = DARK[STATUS_TOKENS[status]]
    edge = ' stroke-dasharray="5 4"' if status in DASHED else ""
    label = f"{title}, {status}"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{escape(label)}">',
        f'  <title>{escape(label)}</title>',
        # The plate is what makes the card independent of the page. It is
        # opaque and it reaches every edge, so a light README and a dark one
        # get the same card rather than the same card and a stripe of page.
        f'  <rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" '
        f'rx="{PLATE_RADIUS}" fill="{DARK["bg"]}"/>',
        f'  <rect x="{number(INSET)}" y="{number(INSET)}" '
        f'width="{number(WIDTH - INSET * 2)}" '
        f'height="{number(HEIGHT - INSET * 2)}" rx="{CARD_RADIUS}" '
        f'fill="{DARK["surface"]}" stroke="{DARK["border"]}" '
        f'stroke-width="1"{edge}/>',
        # The tile is filled in bg rather than surface so the letters have
        # something darker than the card itself to stand on.
        f'  <rect x="{number(TILE_X)}" y="{TILE_Y}" width="{TILE_SIDE}" '
        f'height="{TILE_SIDE}" rx="{TILE_RADIUS}" fill="{DARK["bg"]}" '
        f'stroke="{DARK["border"]}" stroke-width="1"/>',
        f'  <text x="{number(CENTRE_X)}" y="{number(TILE_BASELINE)}" '
        f'font-family="{FONT}" font-size="{number(TILE_SIZE)}" '
        f'font-weight="bold" text-anchor="middle" '
        f'fill="{accent}">{escape(letters)}</text>',
        f'  <text x="{number(CENTRE_X)}" y="{TITLE_Y}" font-family="{FONT}" '
        f'font-size="{number(TITLE_SIZE)}" font-weight="bold" '
        f'text-anchor="middle" '
        f'fill="{DARK["text"]}">{escape(title)}</text>',
        f'  <text x="{number(CENTRE_X)}" y="{STATUS_Y}" font-family="{FONT}" '
        f'font-size="{number(STATUS_SIZE)}" letter-spacing="0.4" '
        f'text-anchor="middle" fill="{accent}">{escape(status)}</text>',
        '</svg>',
    ]
    return "\n".join(lines) + "\n"


def contrast_ratio(first, second):
    """WCAG contrast between two "#rrggbb" strings.

    The same arithmetic as ps3tools/shell/theme.py, written out again here for
    the same reason the palette is: this script has to run on a machine with
    nothing installed.
    """
    def luminance(colour):
        channels = []
        for start in (1, 3, 5):
            value = int(colour[start:start + 2], 16) / 255.0
            channels.append(value / 12.92 if value <= 0.03928
                            else ((value + 0.055) / 1.055) ** 2.4)
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    lighter, darker = sorted((luminance(first), luminance(second)),
                             reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def report_contrast(wanted=4.5):
    """Print how every status colour reads, so it is checkable rather than
    taken on trust. A status word nobody can read is a card that says
    nothing, which is what the first drawing of these did."""
    print()
    for status, token in sorted(STATUS_TOKENS.items()):
        colour = DARK[token]
        on_card = contrast_ratio(colour, DARK["surface"])
        on_tile = contrast_ratio(colour, DARK["bg"])
        flag = "" if min(on_card, on_tile) >= wanted else "   TOO LOW"
        print(f"  {status:<14} {token:<9} {on_card:5.2f}:1 on the card, "
              f"{on_tile:5.2f}:1 on the tile{flag}")
    title = contrast_ratio(DARK["text"], DARK["surface"])
    print(f"  {'title':<14} {'text':<9} {title:5.2f}:1 on the card")


def report_widths():
    """Print the longest title against the room there is for it.

    The first version of these cards put the title beside the tile, where it
    did not fit, and nothing said so until eight of them were rendered. This
    is that check, done every run.
    """
    print()
    widest = max((card[2] for card in CARDS), key=len)
    estimate = estimate_width(widest, TITLE_SIZE, bold=True)
    flag = "" if estimate <= TEXT_LIMIT else "   TOO WIDE"
    print(f"  widest title   {widest!r} about {estimate:.0f}px of the "
          f"{number(TEXT_LIMIT)}px a card allows{flag}")
    if estimate > TEXT_LIMIT:
        print("  lower TITLE_SIZE until it fits; the card cannot reflow")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "cards")
    os.makedirs(out_dir, exist_ok=True)
    for name, letters, title, status in CARDS:
        path = os.path.join(out_dir, name)
        body = card_svg(letters, title, status)
        # Written as bytes with explicit newlines, because the default on
        # Windows would translate them and the same script would then produce
        # a different file on a different machine.
        with open(path, "wb") as handle:
            handle.write(body.encode("utf-8"))
        print(f"{name}  {len(body.encode('utf-8'))} bytes")
    report_widths()
    report_contrast()


if __name__ == "__main__":
    main()
