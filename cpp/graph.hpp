#pragma once

#include <vector>

struct BrailleCell {
    wchar_t glyph = L'\u2800';
    short color_pair = 2;
};

using BrailleRows = std::vector<std::vector<BrailleCell>>;

// Build a history graph from one or two live series. With two series, the
// primary series grows upward from the center line and the secondary grows
// downward. `mirror_secondary` keeps RX visually mirrored below the divider.
BrailleRows braille_graph(const std::vector<double>& primary,
                          const std::vector<double>& secondary,
                          int cells_w, int cells_h,
                          double max_primary, double max_secondary,
                          bool mirror_secondary = false);
