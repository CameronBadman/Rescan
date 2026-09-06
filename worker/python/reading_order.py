"""Column-aware ordering using whitespace gutters; coordinates remain in output."""

def order_lines(lines):
    if len(lines) < 2:
        return lines
    # A vertical whitespace gutter separates columns. Full-width headings are
    # separated by horizontal cuts first when no vertical cut is possible.
    for axis in (0, 1):
        sorted_lines = sorted(lines, key=lambda line: line["bbox"][axis])
        edge = sorted_lines[0]["bbox"][axis + 2]
        candidates = []
        for i, line in enumerate(sorted_lines[1:], 1):
            gap = line["bbox"][axis] - edge
            if gap > (20 if axis == 0 else 12):
                candidates.append((gap, i))
            edge = max(edge, line["bbox"][axis + 2])
        if candidates:
            _, split = max(candidates)
            return order_lines(sorted_lines[:split]) + order_lines(sorted_lines[split:])
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
