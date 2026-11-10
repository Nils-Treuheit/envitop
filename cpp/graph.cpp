#include "graph.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace {

short intensity_pair(double ratio, short base) {
    if (!std::isfinite(ratio)) ratio = 0.0;
    const int tier = std::clamp(static_cast<int>(ratio * 4.0), 0, 3);
    return static_cast<short>(base + tier);
}

void set_dot(std::vector<std::vector<std::uint8_t>>& masks,
             std::vector<std::vector<short>>& colors,
             int x, int y, short color) {
    const int width = static_cast<int>(masks.empty() ? 0 : masks.front().size() * 2);
    const int height = static_cast<int>(masks.size() * 4);
    if (x < 0 || x >= width || y < 0 || y >= height) return;
    const int cell_x = x / 2;
    const int cell_y = y / 4;
    const int dot_x = x % 2;
    const int dot_y = y % 4;
    static constexpr std::uint8_t left_bits[] = {0x01, 0x02, 0x04, 0x40};
    static constexpr std::uint8_t right_bits[] = {0x08, 0x10, 0x20, 0x80};
    const auto bit = dot_x == 0 ? left_bits[dot_y] : right_bits[dot_y];
    masks[cell_y][cell_x] |= bit;
    colors[cell_y][cell_x] = color;
}

void line_segment(std::vector<std::vector<std::uint8_t>>& masks,
                  std::vector<std::vector<short>>& colors,
                  int x0, int y0, int x1, int y1, short color) {
    const int dx = std::abs(x1 - x0);
    const int sx = x0 < x1 ? 1 : -1;
    const int dy = -std::abs(y1 - y0);
    const int sy = y0 < y1 ? 1 : -1;
    int error = dx + dy;
    for (;;) {
        set_dot(masks, colors, x0, y0, color);
        if (x0 == x1 && y0 == y1) break;
        const int twice = 2 * error;
        if (twice >= dy) { error += dy; x0 += sx; }
        if (twice <= dx) { error += dx; y0 += sy; }
    }
}

void plot_series(const std::vector<double>& values,
                 std::vector<std::vector<std::uint8_t>>& masks,
                 std::vector<std::vector<short>>& colors,
                 int pixel_width, int pixel_height,
                 int lane_top, int lane_bottom,
                 double maximum, bool mirror,
                 short color_base) {
    if (values.empty() || pixel_width <= 0 || pixel_height <= 0) return;
    const int start = static_cast<int>(values.size()) > pixel_width
        ? static_cast<int>(values.size()) - pixel_width : 0;
    const int count = static_cast<int>(values.size()) - start;
    if (count <= 0) return;
    if (!(maximum > 0.0) || !std::isfinite(maximum)) {
        maximum = 0.0;
        for (int i = start; i < static_cast<int>(values.size()); ++i)
            if (std::isfinite(values[i])) maximum = std::max(maximum, values[i]);
        if (!(maximum > 0.0)) maximum = 1.0;
    }
    const int lane_height = std::max(1, lane_bottom - lane_top + 1);
    int last_x = 0;
    int last_y = 0;
    bool have_last = false;
    for (int i = 0; i < count; ++i) {
        const int x = count == 1 ? pixel_width - 1
                                 : static_cast<int>(std::llround(
                                       static_cast<double>(i) * (pixel_width - 1) / (count - 1)));
        const double value = std::isfinite(values[start + i])
            ? std::max(0.0, values[start + i]) : 0.0;
        const double ratio = std::clamp(value / maximum, 0.0, 1.0);
        const int offset = static_cast<int>(std::llround(ratio * std::max(0, lane_height - 1)));
        const int y = mirror ? lane_top + offset : lane_bottom - offset;
        const short color = intensity_pair(ratio, color_base);
        if (have_last) line_segment(masks, colors, last_x, last_y, x, y, color);
        else set_dot(masks, colors, x, y, color);
        last_x = x;
        last_y = y;
        have_last = true;
    }
}

}  // namespace

BrailleRows braille_graph(const std::vector<double>& primary,
                          const std::vector<double>& secondary,
                          int cells_w, int cells_h,
                          double max_primary, double max_secondary,
                          bool mirror_secondary) {
    cells_w = std::max(0, cells_w);
    cells_h = std::max(0, cells_h);
    std::vector<std::vector<std::uint8_t>> masks(
        cells_h, std::vector<std::uint8_t>(cells_w, 0));
    std::vector<std::vector<short>> colors(
        cells_h, std::vector<short>(cells_w, 2));
    const int pixel_width = cells_w * 2;
    const int pixel_height = cells_h * 4;
    if (pixel_width > 0 && pixel_height > 0) {
        if (!secondary.empty()) {
            const int divider = pixel_height / 2;
            plot_series(primary, masks, colors, pixel_width, pixel_height,
                        0, std::max(0, divider - 1), max_primary, false, 2);
            plot_series(secondary, masks, colors, pixel_width, pixel_height,
                        std::min(pixel_height - 1, divider), pixel_height - 1,
                        max_secondary, mirror_secondary, 6);
        } else {
            plot_series(primary, masks, colors, pixel_width, pixel_height,
                        0, pixel_height - 1, max_primary, false, 2);
        }
    }
    BrailleRows result(cells_h, std::vector<BrailleCell>(cells_w));
    for (int row = 0; row < cells_h; ++row) {
        for (int col = 0; col < cells_w; ++col) {
            result[row][col].glyph = static_cast<wchar_t>(0x2800 + masks[row][col]);
            result[row][col].color_pair = colors[row][col];
        }
    }
    return result;
}
