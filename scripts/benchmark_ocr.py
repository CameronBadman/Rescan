"""Run inside the worker image: python benchmark_ocr.py /tmp/report-dir."""
import json
import resource
import sys
import time
from pathlib import Path
sys.path.insert(0, "/app/python")
from recognize import recognize
from PIL import Image, ImageDraw, ImageFont


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        nxt = [i]
        for j, right in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j-1] + (left != right)))
        row = nxt
    return row[-1]


destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
expected = "Jane Doe\nSoftware Engineer\nJava and PostgreSQL"
image = Image.new("RGB", (1100, 350), "white")
draw = ImageDraw.Draw(image)
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 42)
draw.multiline_text((40, 35), expected, fill="black", font=font, spacing=25)
source = destination / "synthetic-resume.png"
image.save(source)
started = time.monotonic()
result = recognize(source)
report = {"fixture": "synthetic printed English", "expected": expected, "actual": result["text"],
          "characterErrorRate": distance(expected, result["text"]) / len(expected),
          "caseInsensitiveCharacterErrorRate": distance(expected.casefold(), result["text"].casefold()) / len(expected),
          "elapsedSeconds": time.monotonic() - started,
          "peakRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
          "modelRevision": result["modelRevision"]}
(destination / "benchmark.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
