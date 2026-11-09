"""Writes the docs diagrams as SVGs. Run: python3 docs/assets/diagrams.py docs/assets

The style follows the ProofGuard note drawings: grey canvas, dashed groups with
bold titles, white boxes, the gateway in red and owner rules as blue arrows.
"""
import sys
from pathlib import Path
from xml.sax.saxutils import escape

INK, RED, BLUE, GREY = "#111", "#b92232", "#195bbb", "#999"


class Svg:
    def __init__(self, width, height, title):
        self.width, self.height, self.title, self.parts = width, height, title, []

    def text(self, x, y, value, size=19, weight=400, fill=INK, anchor="middle"):
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" '
                          f'font-weight="{weight}" fill="{fill}">{escape(value)}</text>')

    def group(self, x, y, width, height, title):
        self.text(x + 10, y - 23, title, 22, 700, anchor="start")
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="#ebebeb" '
                          'stroke="#aaa" stroke-width="1" stroke-dasharray="7 6"/>')

    def box(self, x, y, width, height, title, lines=(), kind="plain", size=24):
        fill, stroke, stroke_width, color = {
            "plain": ("white", "#888", 1, INK),
            "gateway": ("#fbedef", RED, 2, RED),
            "effect": ("#d5d5d5", INK, 1, INK),
        }[kind]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="{stroke_width}"/>')
        middle = x + width / 2
        total = size + 28 * len(lines)
        top = y + (height - total) / 2 + size * 0.85
        self.text(middle, top, title, size, 700, color)
        for index, line in enumerate(lines):
            self.text(middle, top + 34 + 28 * index, line)

    def arrow(self, path, color=INK, dashed=False):
        marker = {INK: "black", RED: "red", BLUE: "blue", GREY: "gray"}[color]
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"{dash} '
                          f'marker-end="url(#arrow-{marker})"/>')

    def no_network(self, x, y, label):
        self.parts.append(f'<g transform="translate({x} {y})" fill="none" stroke="#555" stroke-width="1.5">'
                          '<circle r="14"/><ellipse rx="6" ry="14"/><path d="M-14 0H14M-11-8H11M-11 8H11"/>'
                          f'<path d="M-14 14L14-14" stroke="{RED}" stroke-width="3"/></g>')
        self.text(x + 30, y + 6, label, 16, anchor="start")

    def save(self, path):
        markers = "".join(
            f'<marker id="arrow-{name}" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto">'
            f'<path d="M1 1L8 5L1 9" fill="none" stroke="{color}" stroke-width="1.4"/></marker>'
            for name, color in (("black", INK), ("red", RED), ("blue", BLUE), ("gray", GREY)))
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" role="img" aria-label="{escape(self.title)}">'
            f'<title>{escape(self.title)}</title><defs>{markers}</defs>'
            f'<g font-family="Arial, Helvetica, sans-serif"><rect width="{self.width}" height="{self.height}" '
            f'fill="#ebebeb"/>{"".join(self.parts)}</g></svg>\n')


def overview(out):
    s = Svg(1200, 435, "ProofGuard separates agent planning from owner-side execution")
    s.group(20, 65, 235, 350, "Harness (pi)")
    s.group(340, 65, 650, 350, "ProofGuard foundation")
    s.box(40, 90, 195, 65, "Task + results", size=21)
    s.arrow("M137 164V190")
    s.box(40, 200, 195, 100, "Model", ["writes a program"])
    s.no_network(60, 352, "No network")
    s.arrow("M235 250H365")
    s.text(300, 230, "Bend2 program", 16)
    s.box(400, 90, 540, 55, "Plugins: the owner's laws", size=21)
    s.arrow("M487 145V175", BLUE)
    s.arrow("M845 145V175", BLUE)
    s.box(365, 175, 245, 130, "Check", ["Bend + BendTT", "types + every law"])
    s.arrow("M610 240H725")
    s.text(667, 221, "Accepted", 17)
    s.box(725, 175, 240, 130, "Gateway", ["Run with private inputs", "Apply the batch"], "gateway")
    s.arrow("M965 240H1040", RED)
    s.box(1040, 195, 140, 90, "Effects", ["file writes"], "effect", 20)
    s.arrow("M487 305V350")
    s.text(487, 382, "Rejected → nothing runs")
    s.save(out / "overview.svg")


def pipeline(out):
    s = Svg(1200, 360, "The steps from a proposal to its effects")
    s.group(20, 65, 1160, 275, "From proposal to effect")
    steps = [("1. Receive", ["from the outbox"], "plain"),
             ("2. Verify", ["types + plugin laws"], "plain"),
             ("3. Execute", ["private inputs"], "plain"),
             ("4. Apply", ["validate the batch"], "gateway"),
             ("5. Report", ["result to /results"], "plain")]
    for index, (title, lines, kind) in enumerate(steps):
        x = 45 + index * 228
        s.box(x, 95, 196, 110, title, lines, kind, 22)
        if index < 4:
            s.arrow(f"M{x + 196} 150H{x + 228}", RED if index == 3 else INK)
    s.arrow("M371 205V250")
    s.text(371, 282, "Rejected → nothing runs,")
    s.text(371, 308, "the agent gets a status")
    s.save(out / "pipeline.svg")


def meijer(out):
    s = Svg(1200, 520, "How the Meijer plugin fits into the pipeline")
    s.group(20, 65, 1160, 175, "Meijer plugin")
    s.box(40, 90, 250, 125, "check.mjs", ["parses the program,", "traces data flows"], size=22)
    s.arrow("M290 152H330")
    s.box(330, 90, 250, 125, "Summary", ["effects +", "dependencies"], size=22)
    s.arrow("M580 152H620")
    s.box(620, 90, 250, 125, "Laws", ["data_safe,", "outcome_safe, …"], size=22)
    s.box(910, 90, 250, 125, "dispatch guard", ["approvals +", "byte budgets"], size=22)
    s.group(20, 320, 1160, 175, "ProofGuard pipeline")
    s.box(40, 360, 250, 100, "Proposed program", size=21)
    s.arrow("M290 410H620")
    s.text(455, 392, "Bend2 program", 16)
    s.box(620, 345, 250, 130, "Verify", ["Bend + BendTT"], size=22)
    s.arrow("M870 410H910")
    s.box(910, 345, 250, 130, "Gateway", ["run main, guard,", "apply the batch"], "gateway", 22)
    s.arrow("M745 215V345", BLUE)
    s.arrow("M1035 215V345", BLUE)
    s.save(out / "meijer.svg")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    overview(target)
    pipeline(target)
    meijer(target)
