"""Synthetic OCR regression benchmark; run inside a resource-limited worker image.

podman run --rm --network none --cpus 1 --memory 4g --entrypoint /app/venv/bin/python \
  -v ./scripts/benchmark_ocr.py:/app/benchmark_ocr.py:ro,Z IMAGE /app/benchmark_ocr.py
Produces JSON on stdout, not a claim about real-resume accuracy.
"""
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.path.insert(0, "/app/python")
from recognize import recognize


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def normalized(text):
    return " ".join(text.casefold().split())


if __name__ == "__main__":
    expected = "Jane Doe\nSoftware Engineer\nJava and PostgreSQL"
    reports = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rescan-document-benchmark-") as directory:
        root = Path(directory)
        for name, size, blur, extension in [
            ("clean-large", 42, 0, "png"),
            ("clean-small", 26, 0, "png"),
            ("blurred", 32, 0.7, "png"),
            ("jpeg", 36, 0, "jpg"),
            ("tiff", 36, 0, "tiff"),
            ("multipage", 36, 0, "tiff"),
        ]:
            image = Image.new("RGB", (1200, 450), "white")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
            draw.multiline_text((50, 50), expected, fill="black", font=font, spacing=25)
            if blur:
                image = image.filter(ImageFilter.GaussianBlur(blur))
            source = root / (name + "." + extension)
            if name == "multipage":
                image.save(source, save_all=True, append_images=[image])
            else:
                image.save(source)
            before = time.monotonic()
            parsed = recognize(source)
            actual = parsed["text"]
            reference = expected + ("\n\n" + expected if name == "multipage" else "")
            reports.append({"fixture": name, "seconds": time.monotonic() - before,
                            "expected": reference, "actual": actual,
                            "normalizedCER": distance(normalized(reference), normalized(actual)) / len(normalized(reference))})
    print(json.dumps({"syntheticOnly": True, "elapsedSeconds": time.monotonic() - started,
                      "peakRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      "meanNormalizedCER": sum(r["normalizedCER"] for r in reports) / len(reports),
                      "fixtures": reports}, indent=2))
