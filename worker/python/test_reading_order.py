import unittest
from reading_order import order_lines

class ReadingOrderTests(unittest.TestCase):
    def test_columns(self):
        lines = [{"text": "right", "bbox": [200, 0, 300, 10]},
                 {"text": "left bottom", "bbox": [0, 30, 100, 40]},
                 {"text": "left top", "bbox": [0, 0, 100, 10]}]
        self.assertEqual([x["text"] for x in order_lines(lines)], ["left top", "left bottom", "right"])

    def test_full_width_heading(self):
        lines = [{"text": "heading", "bbox": [0, 0, 300, 10]},
                 {"text": "right", "bbox": [200, 40, 300, 50]},
                 {"text": "left", "bbox": [0, 40, 100, 50]}]
        self.assertEqual([x["text"] for x in order_lines(lines)], ["heading", "left", "right"])

if __name__ == "__main__":
    unittest.main()
