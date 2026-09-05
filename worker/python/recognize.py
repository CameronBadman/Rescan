"""Recognize printed English text from image pages using a ViT encoder/decoder."""
import json
import os
import sys
from pathlib import Path
from reading_order import order_lines


def recognize(source):
    import numpy as np
    import torch
    from PIL import Image, ImageOps, ImageSequence
    from paddleocr import TextDetection
    from transformers import TrOCRProcessor, VisionEncoderDecoderModel

    torch.set_num_threads(4)
    Image.MAX_IMAGE_PIXELS = int(os.getenv("MAX_PAGE_PIXELS", "12000000"))
    max_pages = int(os.getenv("MAX_PAGES", "50"))
    root = os.getenv("VIT_MODEL_DIR", "/app/models/recognizer")
    processor = TrOCRProcessor.from_pretrained(root, local_files_only=True)
    model = VisionEncoderDecoderModel.from_pretrained(root, local_files_only=True, use_safetensors=True).eval()
    detector = TextDetection(model_name="PP-OCRv5_mobile_det", model_dir=os.getenv("DETECTOR_MODEL_DIR", "/app/models/detector"), device="cpu", enable_mkldnn=False, cpu_threads=4)
    pages = []
    paths = sorted(source.glob("*.png")) if source.is_dir() else [source]
    for path in paths:
        with Image.open(path) as original:
            for frame in ImageSequence.Iterator(original):
                if len(pages) >= max_pages:
                    raise ValueError("PAGE_LIMIT")
                image = ImageOps.exif_transpose(frame).convert("RGB")
                if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise ValueError("IMAGE_LIMIT")
                detection = next(iter(detector.predict(np.asarray(image))))
                lines = []
                for polygon in detection["dt_polys"]:
                    points = np.asarray(polygon)
                    x0, y0 = np.maximum(points.min(axis=0).astype(int) - 2, 0)
                    x1, y1 = np.minimum(points.max(axis=0).astype(int) + 2, [image.width, image.height])
                    if x1 <= x0 or y1 <= y0:
                        continue
                    crop = image.crop((int(x0), int(y0), int(x1), int(y1)))
                    pixels = processor(images=crop, return_tensors="pt").pixel_values
                    with torch.inference_mode():
                        tokens = model.generate(pixels, max_new_tokens=256)
                    text = processor.batch_decode(tokens, skip_special_tokens=True)[0].strip()
                    if text:
                        lines.append({"text": text, "bbox": [int(x0), int(y0), int(x1), int(y1)]})
                lines = order_lines(lines)
                page = int(path.stem) if source.is_dir() else len(pages) + 1
                pages.append({"page": page, "width": image.width, "height": image.height, "method": "VIT", "lines": lines, "text": "\n".join(line["text"] for line in lines)})
    revisions = json.loads(Path(__file__).with_name("models.json").read_text())
    return {"text": "\n\n".join(p["text"] for p in pages), "pages": pages,
            "extractionMethod": "VIT", "ocrUsed": True, "warnings": [],
            "modelRevision": revisions["recognizer"]["revision"]}


if __name__ == "__main__":
    result = recognize(Path(sys.argv[1]))
    if len(result["text"].encode("utf-8")) > 10 * 1024 * 1024:
        raise ValueError("TEXT_LIMIT")
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
