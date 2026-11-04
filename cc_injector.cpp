
// cc_injector.cpp
// Build (Ubuntu): g++ -std=c++17 cc_injector.cpp $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) -o cc_injector

#include <iostream>
#include <vector>
#include <deque>
#include <string>
#include <cstring>
#include <algorithm>
#include <cctype>
#include <cerrno>

// POSIX UDP socket (non-blocking)
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/opt.h>
#include <libavutil/frame.h>
#include <libavutil/channel_layout.h> // legacy + new API header
#include <libavutil/error.h>
}

// ======================================================================================
// CEA-608 helpers (text → A/53 cc_data triplets with odd parity, Field 1)
// ======================================================================================

static inline uint8_t cea608_parity(uint8_t c7)
{
    c7 &= 0x7F;
    uint8_t msb = (__builtin_popcount((unsigned)c7) & 1) ? 0x00 : 0x80;
    return c7 | msb;
}

// A/53 cc_data triplet (3 bytes). Use 0xFC for Field 1 (valid=1), 0xFD for Field 2.
static inline void push_cc_triplet(std::vector<uint8_t>& out, uint8_t a, uint8_t b)
{
    const uint8_t header = 0xFC; // Field 1, valid=1
    out.push_back(header);
    out.push_back(cea608_parity(a));
    out.push_back(cea608_parity(b));
}
static inline void push_pair(std::vector<uint8_t>& out, uint8_t a, uint8_t b) { push_cc_triplet(out,a,b); }

// Limit to 32 and send as 608 text pairs (space padded).
// Never paint '|' — that is only an internal line separator (looks like ÷ / bar on TV).
static inline void push_text(std::vector<uint8_t>& out, const std::string& s)
{
    std::string t;
    t.reserve(32);
    for (char ch : s) {
        if (ch == '|') continue;
        t.push_back(ch);
        if (t.size() >= 32) break;
    }
    size_t len = t.size();
    for (size_t i = 0; i < len; i += 2) {
        uint8_t c1 = (uint8_t)t[i];
        uint8_t c2 = (i + 1 < len) ? (uint8_t)t[i + 1] : (uint8_t)' ';
        push_pair(out, c1, c2);
    }
}

// CEA-608 roll-up midrow: RU2=0x25, RU3=0x26, RU4=0x27
static inline uint8_t ru_midrow_cmd(int ru_lines)
{
    if (ru_lines >= 4) return 0x27;
    if (ru_lines == 3) return 0x26;
    return 0x25;
}

// Word-wrap one segment to max_cols. Never splits a word unless the word itself
// is longer than max_cols. Returns ALL lines (no row cap) so callers can roll.
static std::vector<std::string> wrap_segment_to_width(const std::string& p, int max_cols)
{
    std::vector<std::string> out;
    if (max_cols < 8) max_cols = 8;
    if (max_cols > 32) max_cols = 32;
    if (p.empty()) {
        out.push_back("");
        return out;
    }
    if ((int)p.size() <= max_cols) {
        out.push_back(p);
        return out;
    }

    size_t i = 0;
    while (i < p.size()) {
        while (i < p.size() && p[i] == ' ') ++i;
        if (i >= p.size()) break;
        size_t remain = p.size() - i;
        size_t take = std::min<size_t>((size_t)max_cols, remain);
        if (take == (size_t)max_cols && i + take < p.size()) {
            // Break at last space inside the window so words stay whole
            size_t sp = p.rfind(' ', i + take);
            if (sp != std::string::npos && sp > i) {
                take = sp - i; // exclude the space
            }
            // else: no space in window → hard-break (single word > max_cols)
        }
        if (take == 0) take = std::min<size_t>((size_t)max_cols, remain);
        out.push_back(p.substr(i, take));
        i += take;
        while (i < p.size() && p[i] == ' ') ++i;
    }
    if (out.empty()) out.push_back("");
    return out;
}

// Split on '|' (hard line breaks) and/or wrap at max_cols with smart word wrap.
// If max_lines > 0, keep only the last max_lines (live window).
// If max_lines <= 0, return every wrapped line (for sequential roll-up).
// Keeps empty segments so "line1|" (Enter on live_type) starts a blank 2nd row.
static std::vector<std::string> split_caption_lines(const std::string& s, int max_lines, int max_cols = 32)
{
    if (max_cols < 8) max_cols = 8;
    if (max_cols > 32) max_cols = 32;

    std::vector<std::string> parts;
    std::string cur;
    for (char ch : s) {
        if (ch == '|') {
            parts.push_back(cur);
            cur.clear();
        } else {
            cur.push_back(ch);
        }
    }
    parts.push_back(cur);

    std::vector<std::string> out;
    for (const auto& p : parts) {
        auto wrapped = wrap_segment_to_width(p, max_cols);
        out.insert(out.end(), wrapped.begin(), wrapped.end());
    }
    if (max_lines > 0 && (int)out.size() > max_lines) {
        out.erase(out.begin(), out.end() - max_lines);
    }
    if (out.size() == 1 && out[0].empty())
        out.clear();
    return out;
}

// PAC builder for rows 1..15 (CTA-608 Table 53, channel 1)
// indent_col: -1 = style/color PAC; else 0/4/8/12/16/20/24/28
static bool build_pac_for_row(uint8_t row, uint8_t& b1, uint8_t& b2,
                              bool underline=false, uint8_t style=0, int indent_col=-1)
{
    // Row → first byte / upper-half (open-cea Table 53)
    static const uint8_t first_ch1[16] = {
        0, 0x11,0x11, 0x12,0x12, 0x15,0x15, 0x16,0x16,
        0x17,0x17, 0x10, 0x13,0x13, 0x14,0x14
    };
    static const bool upper_half[16] = {
        false,
        true,false, true,false, true,false, true,false,
        true,false, true, true,false, true,false
    };
    if (row < 1 || row > 15) return false;
    b1 = first_ch1[row];
    const uint8_t half = upper_half[row] ? 0x00 : 0x20;
    if (indent_col >= 0) {
        static const int ok[] = {0,4,8,12,16,20,24,28};
        int idx = -1;
        for (int i = 0; i < 8; ++i) if (ok[i] == indent_col) { idx = i; break; }
        if (idx < 0) idx = 0;
        const int indentCode = 8 + idx; // 8..15
        b2 = (uint8_t)(0x40 | half | (indentCode << 1) | (underline ? 1 : 0));
    } else {
        b2 = (uint8_t)(0x40 | half | ((style & 7) << 1) | (underline ? 1 : 0));
    }
    return true;
}

struct RollUpStyle {
    int color = 0;      // 0=white..6=magenta (fg mid-row)
    bool underline = false;
    bool italics = false;
    bool flash = false;
    int bg_color = -1;  // -1=none; 0..7 white..black (optional 608-B extension)
    bool bg_semi = false;
    int indent = -1;    // -1 none; else 0/4/8/12/16/20/24/28
};

static int parse_color_name(const std::string& name)
{
    std::string n = name;
    for (char& c : n) c = (char)std::tolower((unsigned char)c);
    if (n == "white" || n == "w") return 0;
    if (n == "green" || n == "g") return 1;
    if (n == "blue" || n == "b") return 2;
    if (n == "cyan" || n == "c") return 3;
    if (n == "red" || n == "r") return 4;
    if (n == "yellow" || n == "y") return 5;
    if (n == "magenta" || n == "m" || n == "purple") return 6;
    if (n == "black" || n == "k") return 7; // bg only
    return -1;
}

// Mid-row color / italics (CC1). Font / font-size cannot be changed in CEA-608.
static void push_midrow_style(std::vector<uint8_t>& out, const RollUpStyle& st)
{
    // Optional background (EIA-608-B/D extension — not all TVs honor this)
    if (st.bg_color >= 0 && st.bg_color <= 7) {
        push_pair(out, 0x20, 0x20); // leading space (legacy-safe idiom)
        uint8_t second = (uint8_t)(0x20 | ((st.bg_color & 7) << 1) | (st.bg_semi ? 1 : 0));
        push_pair(out, 0x10, second); // BWO..BAS family
    }

    if (st.italics) {
        push_pair(out, 0x11, st.underline ? 0x2F : 0x2E);
    } else {
        static const uint8_t codes[7] = { 0x20, 0x22, 0x24, 0x26, 0x28, 0x2A, 0x2C };
        int c = st.color;
        if (c < 0 || c > 6) c = 0;
        uint8_t code = codes[c];
        if (st.underline) code = (uint8_t)(code | 0x01);
        push_pair(out, 0x11, code);
    }

    if (st.flash) {
        push_pair(out, 0x14, 0x28); // FON
    }
}

// Optional inline style: "[red]text" / "[yellow+u+flash]text" / "[cyan+bgblack]text"
static std::string strip_inline_style(const std::string& line, RollUpStyle& st)
{
    if (line.size() < 3 || line[0] != '[') return line;
    size_t end = line.find(']');
    if (end == std::string::npos || end < 2) return line;
    std::string tag = line.substr(1, end - 1);
    for (char& c : tag) c = (char)std::tolower((unsigned char)c);
    size_t p = 0;
    while (p < tag.size()) {
        size_t q = tag.find('+', p);
        std::string part = (q == std::string::npos) ? tag.substr(p) : tag.substr(p, q - p);
        if (part == "u" || part == "underline") st.underline = true;
        else if (part == "i" || part == "italic" || part == "italics") st.italics = true;
        else if (part == "flash" || part == "f") st.flash = true;
        else if (part.rfind("bg", 0) == 0) {
            std::string bn = part.substr(2);
            bool semi = false;
            if (bn.size() > 4 && bn.compare(bn.size()-4, 4, "semi") == 0) {
                semi = true; bn = bn.substr(0, bn.size()-4);
            }
            if (bn == "none" || bn == "off") st.bg_color = -1;
            else if (bn == "transparent" || bn == "t") {
                // emit BT via special: use bg_color -2? handle in push
                st.bg_color = -2; // transparent sentinel
            } else {
                int col = parse_color_name(bn);
                if (col >= 0) { st.bg_color = col; st.bg_semi = semi; }
            }
        } else {
            int col = parse_color_name(part);
            if (col >= 0 && col <= 6) st.color = col;
        }
        if (q == std::string::npos) break;
        p = q + 1;
    }
    std::string rest = line.substr(end + 1);
    while (!rest.empty() && rest.front() == ' ') rest.erase(rest.begin());
    while (!rest.empty() && rest.back() == ' ') rest.pop_back();
    return rest.empty() ? line : rest;
}

struct RollUpState { bool started=false; int lines=2; };

// CC drain resume (stateless SEI decoders + split at row DER boundaries).
static uint8_t g_cc_resume_row = 15;
static RollUpStyle g_cc_resume_style{};

static bool triplet_at_pair(const std::vector<uint8_t>& q, size_t off, uint8_t a, uint8_t b)
{
    if (off + 3 > q.size()) return false;
    if (q[off] != 0xFC && q[off] != 0xFD) return false;
    return (q[off + 1] & 0x7F) == a && (q[off + 2] & 0x7F) == b;
}

static bool triplet_at_is_printable_text(const std::vector<uint8_t>& q, size_t off)
{
    if (off + 3 > q.size()) return false;
    if (q[off] != 0xFC && q[off] != 0xFD) return false;
    const uint8_t a = q[off + 1] & 0x7F;
    const uint8_t b = q[off + 2] & 0x7F;
    return a >= 0x20 && a <= 0x7E && b >= 0x20 && b <= 0x7E;
}

static size_t align_cc_take_to_row_end(const std::vector<uint8_t>& q, size_t take)
{
    if (take >= q.size() || take < 3) return take;
    take -= take % 3;
    size_t best = 0;
    for (size_t i = 0; i + 3 <= take; i += 3) {
        if (triplet_at_pair(q, i, 0x14, 0x24))
            best = i + 3;
    }
    return best >= 3 ? best : take;
}

static void push_style_attrs(std::vector<uint8_t>& out, const RollUpStyle& st)
{
    if (st.bg_color == -2) {
        push_pair(out, 0x20, 0x20);
        push_pair(out, 0x17, 0x2D); // BT transparent
    }
    push_midrow_style(out, st);
}

static void build_608_row_resume_preamble(std::vector<uint8_t>& out, uint8_t row,
                                            const RollUpStyle& style)
{
    uint8_t p1 = 0, p2 = 0;
    build_pac_for_row(row, p1, p2, style.underline, 0, 0);
    push_pair(out, p1, p2);
    push_style_attrs(out, style);
}

// Roll-up with CR (roll) + PAC + style + text
static void build_ru_update_cc(std::vector<uint8_t>& out, RollUpState& st, const std::string& new_line,
                               uint8_t base_row, const RollUpStyle& style)
{
    out.clear();
    push_pair(out, 0x14, ru_midrow_cmd(st.lines));
    if (st.started) push_pair(out, 0x14, 0x2D); // CR (roll)
    uint8_t p1=0,p2=0;
    build_pac_for_row(base_row, p1, p2, style.underline,
                      style.italics ? 7 : (uint8_t)(style.color > 6 ? 0 : style.color),
                      style.indent);
    push_pair(out, p1, p2);
    push_style_attrs(out, style);
    push_text(out, new_line);
    st.started = true;
}

// Repaint only (DER clears leftover end-of-row chars — needed after backspace)
static void build_ru_repaint_no_roll(std::vector<uint8_t>& out, RollUpState& st, const std::string& line,
                                    uint8_t base_row, const RollUpStyle& style)
{
    out.clear();
    if (!st.started) push_pair(out, 0x14, ru_midrow_cmd(st.lines));
    uint8_t p1=0,p2=0;
    build_pac_for_row(base_row, p1, p2, style.underline,
                      style.italics ? 7 : (uint8_t)(style.color > 6 ? 0 : style.color),
                      style.indent);
    push_pair(out, p1, p2);
    push_style_attrs(out, style);
    push_text(out, line);
    push_pair(out, 0x14, 0x24); // DER
    st.started = true;
}

// Live typing: rewrite same row without rolling.
// always: PAC + text + DER (delete-to-end-of-row).
// on shrink: also EDM once (many STBs ignore spaces); linger must NOT repeat EDM.
static void build_ru_overwrite_cc(std::vector<uint8_t>& out, RollUpState& st, const std::string& line,
                                  uint8_t base_row, const RollUpStyle& style, int& last_paint_len,
                                  bool allow_edm = true)
{
    out.clear();
    const int new_len = (int)std::min<size_t>(line.size(), 32);
    const bool shrink = last_paint_len > new_len;

    // Prefer DER (delete-to-end-of-row) over space-padding. Space tails show up as
    // long blank runs in VLC/soft decoders.
    (void)allow_edm;
    (void)shrink;

    if (!st.started) push_pair(out, 0x14, ru_midrow_cmd(st.lines));

    uint8_t p1=0,p2=0;
    build_pac_for_row(base_row, p1, p2, style.underline,
                      style.italics ? 7 : (uint8_t)(style.color > 6 ? 0 : style.color),
                      style.indent);
    push_pair(out, p1, p2);
    push_style_attrs(out, style);

    std::string painted = line.substr(0, (size_t)new_len);
    if (!painted.empty()) push_text(out, painted);
    push_pair(out, 0x14, 0x24); // DER — clear remainder of row (no visible spaces)

    last_paint_len = new_len;
    st.started = true;
}

// Live-TV CEA-608: keep last N lines on screen (no CR "go up").
// N = st.lines (2..4). Paint only rows that changed.
static void build_ru_live_tv(std::vector<uint8_t>& out, RollUpState& st,
                             const std::vector<std::string>& prev,
                             const std::vector<std::string>& next,
                             uint8_t base_row, const RollUpStyle& style,
                             int& last_bottom_len, bool allow_edm = true)
{
    out.clear();
    if (next.empty()) return;

    int nwin = st.lines;
    if (nwin < 2) nwin = 2;
    if (nwin > 4) nwin = 4;
    const int ncontent = (int)next.size();
    if (ncontent >= 2 && ncontent < nwin)
        nwin = ncontent;

    auto take_window = [&](const std::vector<std::string>& src) {
        std::vector<std::string> w((size_t)nwin);
        size_t si = src.size();
        for (int i = nwin - 1; i >= 0; --i) {
            if (si == 0) break;
            w[(size_t)i] = src[--si].substr(0, 32);
        }
        return w;
    };
    const std::vector<std::string> cur = take_window(next);
    const std::vector<std::string> old = take_window(prev);

    auto paint_row = [&](uint8_t row, const std::string& line, int /*prev_len*/) {
        uint8_t p1=0,p2=0;
        auto pac_col0 = [&]() {
            build_pac_for_row(row, p1, p2, style.underline, 0, 0);
            push_pair(out, p1, p2);
        };
        pac_col0();
        push_pair(out, 0x14, 0x24);
        pac_col0();
        if (!line.empty()) {
            push_style_attrs(out, style);
            push_text(out, line);
        }
        push_pair(out, 0x14, 0x24);
        g_cc_resume_row = row;
        g_cc_resume_style = style;
    };

    if (!st.started) {
        push_pair(out, 0x14, ru_midrow_cmd(nwin));
        st.started = true;
    }
    (void)allow_edm;

    int visible_lines = 0;
    for (int i = 0; i < nwin; ++i)
        if (!cur[(size_t)i].empty()) ++visible_lines;
    const bool full_window = visible_lines >= 2;

    const bool first = prev.empty();
    for (int i = 0; i < nwin; ++i) {
        const bool same = (!first && cur[(size_t)i] == old[(size_t)i]);
        if (!full_window) {
            if (same) continue;
        } else if (same && cur[(size_t)i].empty() && old[(size_t)i].empty()) {
            continue;
        }
        if (cur[(size_t)i].empty() && old[(size_t)i].empty()) continue;
        int row = (int)base_row - (nwin - 1 - i);
        if (row < 1) row = 1;
        if (row > 15) row = 15;
        int prev_len = (int)old[(size_t)i].size();
        if (i == nwin - 1 && last_bottom_len > prev_len) prev_len = last_bottom_len;
        if (cur[(size_t)i].empty())
            paint_row((uint8_t)row, "", prev_len > 0 ? prev_len : 32);
        else
            paint_row((uint8_t)row, cur[(size_t)i],
                      prev_len > (int)cur[(size_t)i].size() ? prev_len : (int)cur[(size_t)i].size());
    }

    last_bottom_len = (int)cur.back().size();
}

static std::string join_caption_lines(const std::vector<std::string>& lines)
{
    std::string s;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) s.push_back('|');
        s += lines[i];
    }
    return s;
}

// Keep-alive for linger re-SEI: null CC pairs only.
// Never replay EDM/CR/DER/708 DefineWindow — those redraws are what flicker on STBs.
static void build_cc_null_hold(std::vector<uint8_t>& out, int pairs = 3)
{
    out.clear();
    if (pairs < 1) pairs = 1;
    if (pairs > 10) pairs = 10;
    for (int i = 0; i < pairs; ++i)
        push_pair(out, 0x00, 0x00);
}

// Erase Displayed Memory — clears on-screen captions (send multiple control pairs).
// Also PAC+DER every window row: some players (VLC) leave trailing chars after EDM alone.
static void build_cc_clear(std::vector<uint8_t>& out, uint8_t base_row = 15, int nwin = 2)
{
    out.clear();
    if (nwin < 2) nwin = 2;
    if (nwin > 4) nwin = 4;
    // CC1 Field 1: EDM / ENM — repeated so players latch
    push_pair(out, 0x14, 0x2C); // EDM
    push_pair(out, 0x14, 0x2C);
    push_pair(out, 0x14, 0x2E); // ENM
    push_pair(out, 0x14, 0x2C); // EDM
    for (int i = 0; i < nwin; ++i) {
        int row = (int)base_row - (nwin - 1 - i);
        if (row < 1) row = 1;
        if (row > 15) row = 15;
        uint8_t p1 = 0, p2 = 0;
        build_pac_for_row((uint8_t)row, p1, p2, false, 0, -1);
        push_pair(out, p1, p2);
        push_pair(out, 0x14, 0x24); // DER — wipe remainder of this row
    }
    push_pair(out, 0x14, 0x2C); // final EDM
    push_pair(out, 0x00, 0x00);
}

// Pop-on (optional)
static void build_popon_cc(std::vector<uint8_t>& out, const std::string& line)
{
    out.clear();
    push_pair(out, 0x14, 0x20); // RCL
    uint8_t p1=0,p2=0; if (build_pac_for_row(15,p1,p2)) push_pair(out,p1,p2);
    push_text(out, line);
    push_pair(out, 0x14, 0x2F); // EOC
}

// Two-line pop-on: both rows in non-display memory, one EOC (TV-safe).
static void build_popon_window(std::vector<uint8_t>& out, const std::vector<std::string>& lines,
                               uint8_t base_row, const RollUpStyle& style, int max_rows)
{
    out.clear();
    if (lines.empty()) return;
    if (max_rows < 1) max_rows = 1;
    if (max_rows > 4) max_rows = 4;
    int n = (int)lines.size();
    if (n > max_rows) n = max_rows;
    const size_t first = lines.size() - (size_t)n;
    push_pair(out, 0x14, 0x20);
    push_pair(out, 0x14, 0x2E);
    for (int i = 0; i < n; ++i) {
        int row = (int)base_row - (n - 1 - i);
        if (row < 1) row = 1;
        if (row > 15) row = 15;
        const std::string line = lines[first + (size_t)i].substr(0, 32);
        uint8_t p1 = 0, p2 = 0;
        build_pac_for_row((uint8_t)row, p1, p2, style.underline, 0, 0);
        push_pair(out, p1, p2);
        if (!line.empty()) {
            push_style_attrs(out, style);
            push_text(out, line);
        }
        push_pair(out, 0x14, 0x24);
        g_cc_resume_row = (uint8_t)row;
        g_cc_resume_style = style;
    }
    push_pair(out, 0x14, 0x2F);
}

// ======================================================================================
// Minimal CEA-708 (DTVCC) Service 1 — window + pen size/font/color + text
// Carried in A/53 as cc_type 3 (START) / 2 (DATA). Enables font size (unlike 608).
// ======================================================================================

enum CcMode : int { CC_MODE_608 = 1, CC_MODE_708 = 2, CC_MODE_BOTH = 3 };

struct Pen708 {
    int size = 1;      // 0=small 1=standard 2=large
    int font = 0;      // 0=default .. 7=smallcaps
    int color608 = 0;  // reuse 608 color index → RGB
    bool underline = false;
    bool italics = false;
    int bg_black = 1;  // solid black background
    int rows = 2;      // window rows (2..4), match --rows
};

static void cea708_color_from_608(int c, int& r, int& g, int& b)
{
    // 2-bit channel values 0..3
    switch (c) {
        case 1: r=0; g=3; b=0; break; // green
        case 2: r=0; g=0; b=3; break; // blue
        case 3: r=0; g=3; b=3; break; // cyan
        case 4: r=3; g=0; b=0; break; // red
        case 5: r=3; g=3; b=0; break; // yellow
        case 6: r=3; g=0; b=3; break; // magenta
        default: r=3; g=3; b=3; break; // white
    }
}

static inline uint8_t cea708_color_byte(int opacity, int r, int g, int b)
{
    return (uint8_t)(((opacity & 3) << 6) | ((r & 3) << 4) | ((g & 3) << 2) | (b & 3));
}

// DTVCC bytes have NO CEA-608 parity
static inline void push_dtvcc_pair(std::vector<uint8_t>& out, uint8_t a, uint8_t b, bool start)
{
    out.push_back(start ? (uint8_t)0xFF : (uint8_t)0xFE); // valid + type 3/2
    out.push_back(a);
    out.push_back(b);
}

static void pack_dtvcc_packet_to_triplets(std::vector<uint8_t>& out, const std::vector<uint8_t>& pkt)
{
    if (pkt.empty()) return;
    for (size_t i = 0; i < pkt.size(); i += 2) {
        uint8_t a = pkt[i];
        uint8_t b = (i + 1 < pkt.size()) ? pkt[i + 1] : (uint8_t)0x00;
        push_dtvcc_pair(out, a, b, i == 0);
    }
}

// DTVCC Caption Channel Packet: [seq:2|size:6] + (size*2-1) bytes data
static std::vector<uint8_t> make_dtvcc_packet(uint8_t seq, const std::vector<uint8_t>& data)
{
    std::vector<uint8_t> pkt;
    // pad data to odd length (size*2-1 is always odd)
    std::vector<uint8_t> d = data;
    if ((d.size() % 2) == 0) d.push_back(0x00);
    // size*2 - 1 = d.size() → size = (d.size()+1)/2
    int psz = (int)(d.size() + 1) / 2;
    if (psz < 1) psz = 1;
    if (psz > 63) psz = 63;
    // trim if too large
    size_t max_data = (size_t)psz * 2 - 1;
    if (d.size() > max_data) d.resize(max_data);
    while ((d.size() % 2) == 0) d.push_back(0x00);
    psz = (int)(d.size() + 1) / 2;
    pkt.push_back((uint8_t)(((seq & 3) << 6) | (psz & 0x3f)));
    pkt.insert(pkt.end(), d.begin(), d.end());
    return pkt;
}

static std::vector<uint8_t> make_service_block(int service, const std::vector<uint8_t>& block)
{
    std::vector<uint8_t> sb;
    int sz = (int)std::min<size_t>(block.size(), 31);
    if (service < 1) service = 1;
    if (service > 6) service = 1; // keep simple (no extended)
    sb.push_back((uint8_t)(((service & 7) << 5) | (sz & 0x1f)));
    sb.insert(sb.end(), block.begin(), block.begin() + sz);
    return sb;
}

static uint8_t g_dtvcc_seq = 0;

// Build cc_data triplets for a 708 caption (may be large — caller may trim by cc_count)
static void build_708_caption_cc(std::vector<uint8_t>& out, const std::vector<std::string>& lines,
                                 const Pen708& pen, bool clear_only)
{
    out.clear();
    std::vector<uint8_t> blk;

    if (clear_only) {
        blk.push_back(0x88); // CLW
        blk.push_back(0xFF); // all windows
        auto sb = make_service_block(1, blk);
        auto pkt = make_dtvcc_packet(g_dtvcc_seq++ & 3, sb);
        pack_dtvcc_packet_to_triplets(out, pkt);
        // complete previous via empty null packet
        std::vector<uint8_t> nullb{0x00};
        auto np = make_dtvcc_packet(g_dtvcc_seq++ & 3, nullb);
        pack_dtvcc_packet_to_triplets(out, np);
        return;
    }

    // ---- Packet 1: DefineWindow0 + SPA + SPC ----
    blk.clear();
    int win_rows = pen.rows;
    if (win_rows < 2) win_rows = 2;
    if (win_rows > 4) win_rows = 4;
    // DF0 DefineWindow0 (0x98) + 6 param bytes — bottom-center, N rows, 32 cols, visible
    blk.push_back(0x98);
    blk.push_back(0x38); // V=1 R=1 C=1 PRIOR=0
    blk.push_back(0xDA); // relative, vert=90
    blk.push_back(0x32); // hor=50
    // anchor LOWER_CENTER=7, row_count = rows-1
    blk.push_back((uint8_t)(0x70 | ((win_rows - 1) & 0x0f)));
    blk.push_back(0x1F); // col_count=31 → 32 cols
    blk.push_back(0x09); // window style 1, pen style 1

    // SPA SetPenAttributes
    int psz = pen.size; if (psz < 0) psz = 0; if (psz > 2) psz = 2;
    int font = pen.font; if (font < 0) font = 0; if (font > 7) font = 7;
    blk.push_back(0x90);
    blk.push_back((uint8_t)((0 << 4) | (1 << 2) | (psz & 3))); // dialog, normal offset, size
    blk.push_back((uint8_t)(((pen.italics ? 1 : 0) << 7) | ((pen.underline ? 1 : 0) << 6) | (font & 7)));

    // SPC SetPenColor
    int r,g,b; cea708_color_from_608(pen.color608, r, g, b);
    blk.push_back(0x91);
    blk.push_back(cea708_color_byte(0, r, g, b));                 // solid fg
    blk.push_back(cea708_color_byte(pen.bg_black ? 0 : 3, 0, 0, 0)); // black solid or transparent
    blk.push_back(0x00); // edge

    {
        auto sb = make_service_block(1, blk);
        auto pkt = make_dtvcc_packet(g_dtvcc_seq++ & 3, sb);
        pack_dtvcc_packet_to_triplets(out, pkt);
    }

    // ---- Packet 2: SPL + text (+ CR between lines) + ETX ----
    blk.clear();
    blk.push_back(0x92); // SPL
    blk.push_back(0x00); // row 0
    blk.push_back(0x00); // col 0

    for (size_t li = 0; li < lines.size(); ++li) {
        if (li) blk.push_back(0x0D); // CR
        for (char ch : lines[li]) {
            unsigned char uc = (unsigned char)ch;
            if (uc < 0x20 || uc > 0x7E || uc == '|') continue;
            blk.push_back((uint8_t)uc);
            if (blk.size() >= 28) break; // leave room for ETX in 31-byte block
        }
        if (blk.size() >= 28) break;
    }
    blk.push_back(0x03); // ETX flush

    {
        // may need to split if > 31
        size_t off = 0;
        while (off < blk.size()) {
            size_t n = std::min<size_t>(31, blk.size() - off);
            std::vector<uint8_t> chunk(blk.begin() + (std::ptrdiff_t)off,
                                       blk.begin() + (std::ptrdiff_t)(off + n));
            auto sb = make_service_block(1, chunk);
            auto pkt = make_dtvcc_packet(g_dtvcc_seq++ & 3, sb);
            pack_dtvcc_packet_to_triplets(out, pkt);
            off += n;
        }
    }

    // Null packet to complete last DTVCC packet assembly
    {
        std::vector<uint8_t> nullb{0x00};
        auto np = make_dtvcc_packet(g_dtvcc_seq++ & 3, nullb);
        pack_dtvcc_packet_to_triplets(out, np);
    }
}

// Keep only complete DTVCC packets (do not cut mid-packet — breaks VLC 708 decode)
static void trim_cc_to_complete_dtvcc(std::vector<uint8_t>& cc, size_t max_bytes)
{
    if (cc.size() <= max_bytes) return;
    size_t keep = 0;
    size_t i = 0;
    while (i + 3 <= cc.size() && i + 3 <= max_bytes) {
        const uint8_t hdr = cc[i];
        const int ctype = hdr & 3;
        const bool valid = (hdr >> 2) & 1;
        if (valid && ctype == 3) {
            // Starting a new DTVCC packet — previous bytes are a safe cut point
            if (i > 0 && i <= max_bytes) keep = i;
        }
        i += 3;
    }
    if (keep == 0) {
        // no full packet boundary; keep only leading 608 (type 0/1) pairs
        i = 0; keep = 0;
        while (i + 3 <= cc.size() && i + 3 <= max_bytes) {
            int ctype = cc[i] & 3;
            if (ctype == 2 || ctype == 3) break;
            keep = i + 3;
            i += 3;
        }
    }
    cc.resize(keep);
}

// Merge 608 + 708 into one cc_data list (cap 31 triplets).
// Priority: complete 608 first (Enter/clear need many pairs). Then pack as many
// complete DTVCC packets as fit — never truncate 708 mid-packet.
[[maybe_unused]]
static void merge_608_708(std::vector<uint8_t>& out, const std::vector<uint8_t>& cc608,
                          const std::vector<uint8_t>& cc708, int mode)
{
    out.clear();
    if (mode == CC_MODE_608) { out = cc608; return; }
    if (mode == CC_MODE_708) {
        out = cc708;
        trim_cc_to_complete_dtvcc(out, 31 * 3);
        return;
    }
    const size_t maxb = 31 * 3;

    std::vector<uint8_t> p608 = cc608;
    if (p608.size() > maxb) {
        p608.resize(maxb);
        p608.resize(p608.size() - (p608.size() % 3));
    }
    out = p608;

    size_t room = (out.size() >= maxb) ? 0 : (maxb - out.size());
    if (room >= 3 && !cc708.empty()) {
        std::vector<uint8_t> p708 = cc708;
        trim_cc_to_complete_dtvcc(p708, room);
        out.insert(out.end(), p708.begin(), p708.end());
    }
}

// Build Annex-B SEI NAL carrying A/53 CEA-608 (GA94)
static std::vector<uint8_t> build_a53_sei_annexb(const std::vector<uint8_t>& cc)
{
    std::vector<uint8_t> nal;
    if (cc.empty() || (cc.size() % 3) != 0) return nal;
    int cc_count = (int)(cc.size() / 3);
    if (cc_count > 31) cc_count = 31;
    const size_t used = (size_t)cc_count * 3;
    const size_t payload_size = 11 + used; // B5.. + marker FF

    std::vector<uint8_t> msg;
    msg.reserve(4 + payload_size + 1);
    msg.push_back(4); // user_data_registered_itu_t_t35
    size_t psz = payload_size;
    while (psz >= 255) { msg.push_back(255); psz -= 255; }
    msg.push_back((uint8_t)psz);
    msg.push_back(0xB5);
    msg.push_back(0x00);
    msg.push_back(0x31);
    msg.push_back('G'); msg.push_back('A'); msg.push_back('9'); msg.push_back('4');
    msg.push_back(0x03);
    msg.push_back((uint8_t)((cc_count & 0x1f) | 0x40));
    msg.push_back(0x00);
    msg.insert(msg.end(), cc.begin(), cc.begin() + (std::ptrdiff_t)used);
    msg.push_back(0xFF);
    msg.push_back(0x80); // rbsp_trailing_bits

    nal.push_back(0x00); nal.push_back(0x00); nal.push_back(0x00); nal.push_back(0x01);
    nal.push_back(0x06); // SEI
    nal.insert(nal.end(), msg.begin(), msg.end());
    return nal;
}

// Find insert point: after AUD/SPS/PPS/SEI, before first VCL NAL
static int find_h264_sei_insert_offset(const uint8_t* data, int size)
{
    int i = 0;
    int insert_at = 0;
    while (i + 4 < size) {
        int sc = 0;
        if (data[i] == 0 && data[i+1] == 0 && data[i+2] == 1) sc = 3;
        else if (data[i] == 0 && data[i+1] == 0 && data[i+2] == 0 && data[i+3] == 1) sc = 4;
        else { ++i; continue; }

        const int nal_start = i;
        const int nal_type = data[i + sc] & 0x1f;
        i += sc + 1;
        int next = i;
        while (next + 3 < size) {
            if (data[next] == 0 && data[next+1] == 0 &&
                (data[next+2] == 1 || (data[next+2] == 0 && next + 3 < size && data[next+3] == 1)))
                break;
            ++next;
        }

        if (nal_type == 9 || nal_type == 7 || nal_type == 8 || nal_type == 6) {
            insert_at = next;
            i = next;
            continue;
        }
        if (nal_type == 1 || nal_type == 5 || nal_type == 2 || nal_type == 3 || nal_type == 4) {
            if (insert_at == 0) insert_at = nal_start;
            break;
        }
        i = next;
    }
    return insert_at;
}

static bool packet_has_annexb(const uint8_t* d, int size)
{
    if (size < 4) return false;
    return (d[0]==0 && d[1]==0 && (d[2]==1 || (d[2]==0 && d[3]==1)));
}

// Cached SEI NAL for identical CC payloads (avoids rebuild every video packet)
struct SeiCache {
    std::vector<uint8_t> cc;
    std::vector<uint8_t> sei;
};
static SeiCache g_sei_cache;

// Insert A53 SEI into an Annex-B H.264 access unit packet (bitstream copy path)
static AVPacket* packet_with_a53_sei(const AVPacket* src, const std::vector<uint8_t>& cc)
{
    if (!src || !src->data || src->size <= 0 || cc.empty()) return nullptr;
    if (!packet_has_annexb(src->data, src->size)) return nullptr;

    if (g_sei_cache.cc != cc) {
        g_sei_cache.cc = cc;
        g_sei_cache.sei = build_a53_sei_annexb(cc);
    }
    const std::vector<uint8_t>& sei = g_sei_cache.sei;
    if (sei.empty()) return nullptr;

    const int off = find_h264_sei_insert_offset(src->data, src->size);
    const int new_size = src->size + (int)sei.size();
    AVPacket* out = av_packet_alloc();
    if (!out) return nullptr;
    if (av_new_packet(out, new_size) < 0) { av_packet_free(&out); return nullptr; }
    if (off > 0) std::memcpy(out->data, src->data, off);
    std::memcpy(out->data + off, sei.data(), sei.size());
    std::memcpy(out->data + off + (int)sei.size(), src->data + off, src->size - off);
    av_packet_copy_props(out, src);
    out->size = new_size;
    return out;
}

// ======================================================================================
// UDP caption input (non-blocking) + logging
// ======================================================================================

struct CaptionInput {
    int fd = -1;                 // UDP socket
    std::string host;
    uint16_t port = 0;
    bool enabled = false;
};

static bool set_nonblock(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return false;
    return (fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0);
}

// Bind udp://host:port; empty host → 127.0.0.1
static bool open_udp_listener(CaptionInput& ci, const std::string& host, uint16_t port) {
    ci.fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (ci.fd < 0) return false;
    int reuse=1; setsockopt(ci.fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    sockaddr_in addr{}; addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    std::string h = host.empty() ? std::string("127.0.0.1") : host;
    if (inet_aton(h.c_str(), &addr.sin_addr) == 0) { close(ci.fd); ci.fd=-1; return false; }

    if (bind(ci.fd, (sockaddr*)&addr, sizeof(addr)) != 0) { close(ci.fd); ci.fd=-1; return false; }
    {
        int rcv = 256 * 1024;
        setsockopt(ci.fd, SOL_SOCKET, SO_RCVBUF, (const char*)&rcv, sizeof(rcv));
    }
    if (!set_nonblock(ci.fd)) { close(ci.fd); ci.fd=-1; return false; }

    ci.host = h; ci.port = port; ci.enabled = true;
    std::cerr << "[cc] Listening for captions on udp://" << ci.host << ":" << ci.port << "\n";
    return true;
}

static inline void ltrim_inplace(std::string& s) {
    size_t i = 0;
    while (i < s.size() && s[i] == ' ') ++i;
    if (i) s.erase(0, i);
}
static inline void rtrim_inplace(std::string& s) {
    size_t i = s.size();
    while (i > 0 && s[i-1] == ' ') --i;
    if (i < s.size()) s.erase(i);
}
static inline void trim_inplace(std::string& s) {
    ltrim_inplace(s); rtrim_inplace(s);
}

// One caption UDP event, kept in arrival order (never coalesced to "latest").
struct CapUdpEvt {
    bool is_clear = false;
    std::string line; // "row1|row2" (empty when is_clear)
};

// Drain every pending datagram into `pending` IN ORDER. Several datagrams that
// land inside one video frame used to collapse to the last one — whole cues
// vanished (e.g. CLEAR + text in the same 20 ms → the text was never painted).
static void udp_drain_events(int fd, std::deque<CapUdpEvt>& pending, bool quiet = false) {
    if (fd < 0) return;
    char buf[4096];
    for (;;) {
        sockaddr_in src{}; socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(fd, buf, sizeof(buf) - 1, 0, (sockaddr*)&src, &slen);
        if (n < 0) break;
        buf[n] = '\0';
        std::string s(buf, (size_t)n);
        for (char& ch : s) if (ch == '\r') ch = '\n';

        std::vector<std::string> lines;
        size_t start = 0;
        while (true) {
            size_t pos = s.find('\n', start);
            std::string seg = (pos == std::string::npos) ? s.substr(start) : s.substr(start, pos - start);
            std::string tmp;
            tmp.reserve(seg.size());
            for (char c : seg) {
                unsigned char uc = (unsigned char)c;
                if (uc >= 0x20 && uc <= 0x7E) tmp.push_back((char)uc);
                else if (uc == '\t') tmp.push_back(' ');
                if (tmp.size() >= 256) break;
            }
            trim_inplace(tmp);
            if (!tmp.empty()) lines.push_back(tmp);
            if (pos == std::string::npos) break;
            start = pos + 1;
        }

        if (lines.empty()) {
            pending.push_back(CapUdpEvt{true, ""});
            if (!quiet) std::cerr << "[cc] recv: CLEAR (empty)\n";
            continue;
        }
        if (lines.size() == 1) {
            std::string up = lines[0];
            for (char& c : up) c = (char)std::toupper((unsigned char)c);
            if (up == "CLEAR" || up == "CLS" || up == "*CLEAR*") {
                pending.push_back(CapUdpEvt{true, ""});
                if (!quiet) std::cerr << "[cc] recv: CLEAR\n";
                continue;
            }
        }
        std::string out_line;
        for (size_t i = 0; i < lines.size(); ++i) {
            if (i) out_line.push_back('|');
            out_line += lines[i];
            if (out_line.size() >= 256) break;
        }
        pending.push_back(CapUdpEvt{false, out_line});
        if (!quiet) {
            int rows = 1;
            for (char c : out_line) if (c == '|') ++rows;
            std::cerr << "[cc] recv: \"" << out_line << "\" (" << rows << " rows)\n";
        }
    }
}

// Legacy "latest wins" reader — kept for reference / tools; the main loop uses
// udp_drain_events so no datagram is ever dropped.
[[maybe_unused]]
static bool udp_get_latest_line_and_log(int fd, std::string& out_line, bool& out_clear, bool quiet = false) {
    if (fd < 0) return false;
    bool got = false;
    out_clear = false;
    char buf[2048];

    for (;;) {
        sockaddr_in src{}; socklen_t slen = sizeof(src);
        ssize_t n = recvfrom(fd, buf, sizeof(buf)-1, 0, (sockaddr*)&src, &slen);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) break;
            else break;
        }
        buf[n] = '\0';
        std::string s(buf, (size_t)n);

        // Normalize CR->LF. Keep '|' in-text so "line1|" (Enter) and "a|b" survive.
        for (char& ch : s) {
            if (ch == '\r') ch = '\n';
        }

        std::vector<std::string> lines;
        size_t start = 0;
        while (true) {
            size_t pos = s.find('\n', start);
            std::string seg = (pos == std::string::npos) ? s.substr(start) : s.substr(start, pos - start);
            // Sanitize each newline row but preserve '|' (multi-line marker)
            std::string tmp;
            tmp.reserve(seg.size());
            for (char c : seg) {
                unsigned char uc = (unsigned char)c;
                if (uc >= 0x20 && uc <= 0x7E) tmp.push_back((char)uc);
                else if (uc == '\t') tmp.push_back(' ');
                if (tmp.size() >= 128) break;
            }
            // trim spaces but not to destroy "a|b" / trailing structure — only edges
            trim_inplace(tmp);
            if (!tmp.empty()) lines.push_back(tmp);
            if (pos == std::string::npos) break;
            start = pos + 1;
        }

        // Whole packet empty / whitespace only → CLEAR
        if (lines.empty()) {
            out_line.clear();
            out_clear = true;
            got = true;
            if (!quiet) std::cerr << "[cc] recv: CLEAR (empty)\n";
            continue;
        }

        // Single-line CLEAR commands
        if (lines.size() == 1) {
            std::string up = lines[0];
            for (char& c : up) c = (char)std::toupper((unsigned char)c);
            if (up == "CLEAR" || up == "CLS" || up == "*CLEAR*") {
                out_line.clear();
                out_clear = true;
                got = true;
                if (!quiet) std::cerr << "[cc] recv: CLEAR\n";
                continue;
            }
        }

        // Join with '|' for existing multi-line roll queue
        out_line.clear();
        for (size_t i = 0; i < lines.size(); ++i) {
            if (i) out_line.push_back('|');
            out_line += lines[i];
            if (out_line.size() >= 128) break;
        }
        out_clear = false;
        got = true;
        if (!quiet) {
            int rows = 1;
            for (char c : out_line) if (c == '|') ++rows;
            std::cerr << "[cc] recv: \"" << out_line << "\" (" << rows << " rows)\n";
        }
    }
    return got;
}

// ======================================================================================
// CLI parsing
// ======================================================================================

static bool parse_cc_udp_arg(const char* s, std::string& host, uint16_t& port) {
    if (!s) return false;
    const char* eq = std::strchr(s, '=');
    if (!eq) return false;
    std::string v(eq+1);
    auto colon = v.rfind(':');
    if (colon == std::string::npos) return false;
    host = v.substr(0, colon);
    std::string ps = v.substr(colon+1);
    int p = std::atoi(ps.c_str());
    if (p <= 0 || p > 65535) return false;
    port = (uint16_t)p;
    return true;
}

static bool parse_venc_arg(const char* s, std::string& enc_name) {
    if (!s) return false;
    const char* eq = std::strchr(s, '=');
    if (!eq) return false;
    enc_name = std::string(eq+1);
    return !enc_name.empty();
}

static bool parse_int_arg(const char* s, const char* key, int& val) {
    if (!s) return false;
    const size_t klen = std::strlen(key);
    if (std::strncmp(s, key, klen) != 0) return false;
    const char* eq = s + klen;
    if (*eq != '=') return false;
    val = std::atoi(eq+1);
    return true;
}

// ======================================================================================
// Audio layout helpers (FFmpeg version-guarded)
// ======================================================================================

[[maybe_unused]]
static void set_audio_layout_from_decoder(AVCodecContext* aencCtx, const AVCodecContext* adecCtx) {
#if LIBAVCODEC_VERSION_MAJOR >= 59
    if (adecCtx->ch_layout.nb_channels > 0) {
        av_channel_layout_copy(&aencCtx->ch_layout, &adecCtx->ch_layout);
    } else {
        av_channel_layout_default(&aencCtx->ch_layout, 2); // stereo
    }
#else
    aencCtx->channels       = adecCtx->channels ? adecCtx->channels : 2;
    aencCtx->channel_layout = adecCtx->channel_layout ? adecCtx->channel_layout : AV_CH_LAYOUT_STEREO;
#endif
}

// ======================================================================================
// Main
// ======================================================================================

static void print_help(const char* prog)
{
    std::cout <<
"Live Caption Encoder (cc_injector)\n"
"  Inject CEA-608 and/or CEA-708 captions into a live MPEG-TS H.264 stream.\n"
"  Video and audio are bitstream-copied (same as source).\n"
"  Captions are A/53 GA94 SEI (608 Field1 and/or 708 DTVCC Service 1).\n"
"\n"
"Usage:\n"
"  " << prog << " -h | --help | help\n"
"  " << prog << " [options]\n"
"  " << prog << " <input> <output> [options]\n"
"\n"
"Arguments:\n"
"  input                   MPEG-TS source (file or UDP URL)\n"
"                          Default: udp://127.0.0.1:5000?...\n"
"  output                  MPEG-TS destination (file or UDP URL)\n"
"                          Default: output.ts\n"
"\n"
"Examples:\n"
"  # Typical live multicast (this host)\n"
"  " << prog << " \\\n"
"    \"udp://239.195.1.17:4900?localaddr=192.168.1.20&reuse=1\" \\\n"
"    \"udp://239.195.11.11:4900?pkt_size=1316&ttl=16&localaddr=192.168.1.20\" \\\n"
"    --cc-udp=127.0.0.1:54001 --rows=2 --timeout_ms=5000 \\\n"
"    --color=yellow --bg=black --indent=4\n"
"\n"
"  # Send captions (to --cc-udp port)\n"
"  printf \"Hello captions\\n\"                 | nc -u -w0 127.0.0.1 54001\n"
"  printf \"Line one\\nLine two\\n\"              | nc -u -w0 127.0.0.1 54001\n"
"  printf \"Alaa Odeh|hjhjh\\n\"                 | nc -u -w0 127.0.0.1 54001\n"
"  printf \"[red]Alaa Odeh\\n\"                  | nc -u -w0 127.0.0.1 54001\n"
"  printf \"[yellow+u]ALERT\\n\"                | nc -u -w0 127.0.0.1 54001\n"
"  printf \"[cyan+i]Hello\\n\"                  | nc -u -w0 127.0.0.1 54001\n"
"  printf \"[white+flash]Flash text\\n\"        | nc -u -w0 127.0.0.1 54001\n"
"  printf \"[red+bgblack]Alert background\\n\"  | nc -u -w0 127.0.0.1 54001\n"
"  printf \"CLEAR\\n\"                          | nc -u -w0 127.0.0.1 54001\n"
"\n"
"General options:\n"
"  -h, --help, help        Show this help and exit\n"
"  --cc-udp=HOST:PORT      Enable UDP caption ingest (required for live text)\n"
"                          Example: --cc-udp=127.0.0.1:54001\n"
"  --bootstrap=0|1         Inject startup \"CC ONLINE\" caption (default: 1)\n"
"\n"
"Caption layout:\n"
"  --rows=N, --ru=N        Roll-up visible lines: 2, 3, or 4 (default: 2)\n"
"  --row=N, --base_row=N   CEA-608 PAC screen row 1..15 (default: 15 = bottom)\n"
"  --width=N, --cols=N     Max chars per line (default 32; CEA-608 max 32).\n"
"                          Long lines auto-wrap to next row; wraps on spaces\n"
"                          so words are not split (unless a word is > width).\n"
"\n"
"Timing / speed:\n"
"  --timeout_ms=N          Auto-CLEAR after N ms (default: 4000; 0 = never)\n"
"  --timeout=N             Alias for --timeout_ms\n"
"  --linger_ms=N           Alias for --timeout_ms\n"
"  --cc_every=N            Inject CC SEI every N video packets (default: 1)\n"
"  --line_gap=N            Packets between multi-line rolls (default: 1)\n"
"  --clear_frames=N        Repeat CLEAR for N packets (default: 12; try 3..8 for speed)\n"
"  --sei_every=N           Re-insert same CC SEI every N video pkts (default: 2).\n"
"                          1 = every pkt; higher = less CPU / mux work when idle.\n"
"  --quiet                 Less stderr logging (faster under load)\n"
"  --fast                  Low latency: cc_every=1, sei_every=2, clear_frames=2,\n"
"                          quieter logs (roll-up kept; use --live for overwrite)\n"
"  --live                  OPTIONAL. Force overwrite mode (no roll-up).\n"
"                          Also sets timeout_ms=0 unless you override it.\n"
"                          Without --live, typing edits (prefix/backspace) still\n"
"                          overwrite one line so RU2 does not show duplicates.\n"
"\n"
"Caption format:\n"
"  --cc=608|708|both     Default: both (608 CC1 + 708 Service 1)\n"
"  --pen_size=small|standard|large   CEA-708 font size (default standard)\n"
"  --font=0..7           CEA-708 font tag (0=default … 7=smallcaps)\n"
"\n"
"Style options (608 + mapped into 708 when enabled):\n"
"  608: color, underline, italics, flash, indent, optional background.\n"
"  708: pen size (--pen_size), font (--font), color/underline/italics.\n"
"\n"
"  --color=NAME            Foreground (default white)\n"
"                          white green blue cyan red yellow magenta\n"
"                          Short: w g b c r y m\n"
"  --bg=NAME               Background (EIA-608-B; many STBs ignore)\n"
"                          none | transparent |\n"
"                          white green blue cyan red yellow magenta black\n"
"                          Append -semi for semi-transparent (e.g. black-semi)\n"
"  --indent=N              Indent columns: 0 4 8 12 16 20 24 28\n"
"  --underline             Underline\n"
"  --italics, --italic     Italics\n"
"  --flash                 Flash / blink (FON)\n"
"\n"
"Inline tags (UDP text; override defaults for that message):\n"
"  [red]text                 Foreground color\n"
"  [yellow+u]text            Yellow + underline\n"
"  [cyan+i]text              Cyan + italics\n"
"  [white+flash]text         Flash\n"
"  [red+bgblack]text         Red on black background\n"
"  [yellow+bgtransparent]x   Transparent background\n"
"  Combine: [r+u+i+flash+bgblack]text\n"
"  Tokens: colors w/g/b/c/r/y/m, +u, +i, +flash,\n"
"          +bgNAME, +bgNAME-semi, +bgtransparent\n"
"\n"
"UDP caption protocol (to --cc-udp port):\n"
"  <text>\\n                One line (max --width ASCII chars; auto word-wrap)\n"
"  <text>                  Same (newline optional) — for live_type keystrokes\n"
"  line1\\nline2\\n          Multi-line roll-up\n"
"  line1|line2\\n           Multi-line roll-up (| separator)\n"
"  CLEAR\\n or CLS\\n        Erase captions now\n"
"  (empty datagram)        Same as CLEAR (live_type sends this on delete-all)\n"
"\n"
"Live typing (OPTIONAL):\n"
"  " << prog << " ... --cc-udp=127.0.0.1:54001 --rows=2 --width=32 --timeout_ms=6000\n"
"  ./live_type.py 127.0.0.1 54001 --rows=2 --width=32 --timeout-ms=6000\n"
"    Match --width/--rows/--timeout-ms with injector.\n"
"    Auto word-wrap at --width · Enter=new line · Backspace · Ctrl+U · Ctrl+C\n"
"\n"
"Legacy / ignored (video is always bitstream-copy):\n"
"  --venc=NAME, --preset=NAME, --bitrate_k=N\n"
"\n"
"Notes:\n"
"  - Captions are in H.264 A/53 SEI (not DVB teletext / extra PID).\n"
"  - Watch the injector OUTPUT directly (not after another transcoder) for lowest delay.\n"
"  - VLC: Subtitle → Closed Captions / CC1 (608) or DTVCC / Service 1 (708).\n"
"  - Input must be H.264 Annex-B in MPEG-TS.\n"
"  - Caption text is ASCII only (0x20-0x7E).\n"
"  - Prefer small UDP fifo (≈120000) + --fast; run via nice -n -5 ./run_injector_fast.sh\n";
}

int main(int argc, char** argv)
{
    av_log_set_level(AV_LOG_ERROR);

    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "-h") || !std::strcmp(argv[i], "--help") || !std::strcmp(argv[i], "help")) {
            print_help(argv[0]);
            return 0;
        }
    }

    // Defaults so `./cc_injector` runs without args
    const char* defaultIn  = "udp://127.0.0.1:5000?timeout=5000000&fifo_size=120000&buffer_size=65536&overrun_nonfatal=1";
    const char* defaultOut = "output.ts";

    const char* inUrl  = defaultIn;
    const char* outUrl = defaultOut;

    // Flags
    bool use_external_udp_captions = false;
    std::string cc_host; uint16_t cc_port = 0;

    // Defaults: prefer libx264 (SEI/GA94 path), bootstrap on, linger 2000ms, RU3
    std::string venc_name = "libx264";
    std::string x264_preset = "ultrafast";
    int bootstrap_enable = 1;
    int linger_ms = 4000;   // display timeout (also --timeout_ms)
    int ru_lines = 2;       // number of CC rows: 2, 3, or 4 (--rows / --ru)
    int base_row = 15;      // CEA-608 screen row (1-15), usually 15 = bottom
    int max_cols = 32;      // chars per line (--width / --cols), CEA-608 max 32
    int cc_every = 1;       // inject every frame = lowest caption latency
    int bitrate_k = 3500;
    int line_gap = 1;       // frames between multi-line rolls (1 = fastest)
    int clear_frames = 12;  // how many packets to repeat CLEAR (faster = lower)
    int sei_every = 2;      // re-send unchanged SEI every N pkts (1 = always)
    bool quiet = false;
    int cc_mode = CC_MODE_BOTH;
    Pen708 pen708{};
    RollUpStyle default_style{};
    bool live_mode = false;
    bool linger_explicit = false;

    int positional = 0;
    for (int i = 1; i < argc; ++i) {
        if (argv[i][0] == '-') {
            if (std::strncmp(argv[i], "--cc-udp=", 9) == 0) {
                if (!parse_cc_udp_arg(argv[i], cc_host, cc_port)) {
                    std::cerr << "Invalid --cc-udp format. Use --cc-udp=HOST:PORT (e.g. --cc-udp=127.0.0.1:54001)\n";
                    return 1;
                }
                use_external_udp_captions = true;
            } else if (std::strncmp(argv[i], "--venc=", 7) == 0) {
                parse_venc_arg(argv[i], venc_name);
            } else if (std::strncmp(argv[i], "--preset=", 9) == 0) {
                x264_preset = argv[i] + 9;
            } else if (parse_int_arg(argv[i], "--bootstrap", bootstrap_enable)) {
                // parsed
            } else if (parse_int_arg(argv[i], "--linger_ms", linger_ms) ||
                       parse_int_arg(argv[i], "--timeout_ms", linger_ms) ||
                       parse_int_arg(argv[i], "--timeout", linger_ms)) {
                linger_explicit = true;
            } else if (parse_int_arg(argv[i], "--ru", ru_lines) ||
                       parse_int_arg(argv[i], "--rows", ru_lines)) {
                // number of roll-up rows
            } else if (parse_int_arg(argv[i], "--row", base_row) ||
                       parse_int_arg(argv[i], "--base_row", base_row)) {
                // PAC base row
            } else if (parse_int_arg(argv[i], "--width", max_cols) ||
                       parse_int_arg(argv[i], "--cols", max_cols)) {
                // chars per line (CEA-608 max 32)
            } else if (parse_int_arg(argv[i], "--cc_every", cc_every)) {
                // parsed
            } else if (parse_int_arg(argv[i], "--line_gap", line_gap)) {
                // frames between | rolls
            } else if (parse_int_arg(argv[i], "--clear_frames", clear_frames)) {
                // CLEAR repeat count
            } else if (parse_int_arg(argv[i], "--sei_every", sei_every)) {
                // how often to re-emit identical CC SEI
            } else if (!std::strcmp(argv[i], "--quiet")) {
                quiet = true;
            } else if (std::strncmp(argv[i], "--cc=", 5) == 0) {
                const char* v = argv[i] + 5;
                if (!std::strcmp(v, "608")) cc_mode = CC_MODE_608;
                else if (!std::strcmp(v, "708")) cc_mode = CC_MODE_708;
                else if (!std::strcmp(v, "both")) cc_mode = CC_MODE_BOTH;
                else { std::cerr << "Unknown --cc= (use 608, 708, or both)\n"; return 1; }
            } else if (std::strncmp(argv[i], "--pen_size=", 11) == 0) {
                const char* v = argv[i] + 11;
                if (!std::strcmp(v, "small") || !std::strcmp(v, "0")) pen708.size = 0;
                else if (!std::strcmp(v, "standard") || !std::strcmp(v, "std") || !std::strcmp(v, "1")) pen708.size = 1;
                else if (!std::strcmp(v, "large") || !std::strcmp(v, "2")) pen708.size = 2;
                else { std::cerr << "Unknown --pen_size= (small|standard|large)\n"; return 1; }
            } else if (parse_int_arg(argv[i], "--font", pen708.font)) {
                // 0..7
            } else if (parse_int_arg(argv[i], "--bitrate_k", bitrate_k)) {
                // parsed
            } else if (std::strncmp(argv[i], "--color=", 8) == 0) {
                int c = parse_color_name(argv[i] + 8);
                if (c < 0 || c > 6) { std::cerr << "Unknown --color (white green blue cyan red yellow magenta)\n"; return 1; }
                default_style.color = c;
            } else if (std::strncmp(argv[i], "--bg=", 5) == 0) {
                std::string bn = argv[i] + 5;
                for (char& c : bn) c = (char)std::tolower((unsigned char)c);
                if (bn == "none" || bn == "off") default_style.bg_color = -1;
                else if (bn == "transparent" || bn == "t") default_style.bg_color = -2;
                else {
                    bool semi = false;
                    if (bn.size() > 5 && bn.compare(bn.size()-5, 5, "-semi") == 0) {
                        semi = true; bn = bn.substr(0, bn.size()-5);
                    }
                    int c = parse_color_name(bn);
                    if (c < 0) { std::cerr << "Unknown --bg= (none transparent white..black, optional -semi)\n"; return 1; }
                    default_style.bg_color = c;
                    default_style.bg_semi = semi;
                }
            } else if (parse_int_arg(argv[i], "--indent", default_style.indent)) {
                // 0/4/8/12/16/20/24/28
            } else if (!std::strcmp(argv[i], "--underline")) {
                default_style.underline = true;
            } else if (!std::strcmp(argv[i], "--italics") || !std::strcmp(argv[i], "--italic")) {
                default_style.italics = true;
            } else if (!std::strcmp(argv[i], "--flash")) {
                default_style.flash = true;
            } else if (!std::strcmp(argv[i], "--live")) {
                live_mode = true;
            } else if (!std::strcmp(argv[i], "--fast")) {
                x264_preset = "ultrafast";
                cc_every = 1;
                line_gap = 2;    // small gap between multi-line rolls
                bitrate_k = 3000;
                clear_frames = 2;
                sei_every = 12;  // rare null keep-alive; avoid redraw flicker
                quiet = true;
                // Do NOT force live_mode: unrelated lines should roll, not EDM-wipe.
                // Prefix/backspace edits still overwrite via is_live_edit().
            } else {
                std::cerr << "Unknown option: " << argv[i] << "\nTry '" << argv[0] << " --help'\n";
                return 1;
            }
        } else {
            if (positional == 0) inUrl = argv[i];
            else if (positional == 1) outUrl = argv[i];
            else {
                std::cerr << "Unexpected argument: " << argv[i] << "\nTry '" << argv[0] << " --help'\n";
                return 1;
            }
            ++positional;
        }
    }
    if (ru_lines < 2) ru_lines = 2;
    if (ru_lines > 4) ru_lines = 4;
    pen708.rows = ru_lines;
    if (base_row < 1) base_row = 1;
    if (base_row > 15) base_row = 15;
    if (max_cols < 8) max_cols = 8;
    if (max_cols > 32) max_cols = 32;
    if (cc_every < 1) cc_every = 1;
    if (line_gap < 1) line_gap = 1;
    if (clear_frames < 1) clear_frames = 1;
    if (clear_frames > 120) clear_frames = 120;
    if (sei_every < 1) sei_every = 1;
    if (sei_every > 30) sei_every = 30;
    if (pen708.font < 0) pen708.font = 0;
    if (pen708.font > 7) pen708.font = 7;
    pen708.color608 = default_style.color;
    pen708.underline = default_style.underline;
    pen708.italics = default_style.italics;
    if (default_style.bg_color >= 0) pen708.bg_black = 1;
    if (default_style.indent >= 0) {
        static const int ok[] = {0,4,8,12,16,20,24,28};
        bool good = false;
        for (int v : ok) if (v == default_style.indent) good = true;
        if (!good) default_style.indent = 0;
    }
    if (bitrate_k < 1000) bitrate_k = 1000;
    if (linger_ms < 0) linger_ms = 0;
    if (live_mode && !linger_explicit) linger_ms = 0;

    // Open input (low-delay options for snappier live captions)
    AVFormatContext* ifmt = nullptr;
    AVDictionary* in_opts = nullptr;
    av_dict_set(&in_opts, "fflags", "nobuffer+discardcorrupt", 0);
    av_dict_set(&in_opts, "flags", "low_delay", 0);
    av_dict_set(&in_opts, "analyzeduration", "500000", 0); // 0.5s max probe
    av_dict_set(&in_opts, "probesize", "200000", 0);
    av_dict_set(&in_opts, "max_delay", "0", 0);
    if (avformat_open_input(&ifmt, inUrl, nullptr, &in_opts) < 0) {
        av_dict_free(&in_opts);
        std::cerr << "open input failed: " << inUrl << "\n"; return 1;
    }
    av_dict_free(&in_opts);
    if (avformat_find_stream_info(ifmt, nullptr) < 0) {
        std::cerr << "find_stream_info failed\n"; return 1;
    }

    int vIdx = av_find_best_stream(ifmt, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    int aIdx = av_find_best_stream(ifmt, AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
    if (vIdx < 0) { std::cerr << "no video stream found\n"; return 1; }

    // Video + audio: bitstream copy (same as source). CEA-608 injected as H.264 SEI.
    if (ifmt->streams[vIdx]->codecpar->codec_id != AV_CODEC_ID_H264) {
        std::cerr << "video bitstream-copy+SEI requires H.264 input\n";
        return 1;
    }

    AVFormatContext* ofmt = nullptr;
    if (avformat_alloc_output_context2(&ofmt, nullptr, "mpegts", outUrl) < 0 || !ofmt) {
        std::cerr << "alloc output failed\n"; return 1;
    }

    AVStream* vout = avformat_new_stream(ofmt, nullptr);
    if (!vout || avcodec_parameters_copy(vout->codecpar, ifmt->streams[vIdx]->codecpar) < 0) {
        std::cerr << "video passthrough setup failed\n"; return 1;
    }
    vout->time_base = ifmt->streams[vIdx]->time_base;
    std::cerr << "[mux] video copy (H.264 bitstream + A53 SEI), "
              << vout->codecpar->width << "x" << vout->codecpar->height
              << " rows=" << ru_lines << " row=" << base_row
              << " width=" << max_cols
              << " timeout_ms=" << linger_ms << " cc_every=" << cc_every
              << " color=" << default_style.color
              << (default_style.underline ? " underline" : "")
              << (default_style.italics ? " italics" : "")
              << (live_mode ? " live=1" : "")
              << " cc=" << (cc_mode == CC_MODE_608 ? "608" : cc_mode == CC_MODE_708 ? "708" : "both")
              << " pen_size=" << pen708.size
              << " clear_frames=" << clear_frames << "\n";

    AVStream* aout = nullptr;
    if (aIdx >= 0) {
        aout = avformat_new_stream(ofmt, nullptr);
        if (!aout || avcodec_parameters_copy(aout->codecpar, ifmt->streams[aIdx]->codecpar) < 0) {
            std::cerr << "audio passthrough setup failed; continuing video-only\n";
            aout = nullptr;
        } else {
            aout->time_base = ifmt->streams[aIdx]->time_base;
            std::cerr << "[mux] audio passthrough enabled\n";
        }
    }
    ofmt->max_interleave_delta = 0;

    if (!(ofmt->oformat->flags & AVFMT_NOFILE)) {
        int oret = avio_open(&ofmt->pb, outUrl, AVIO_FLAG_WRITE);
        if (oret < 0) {
            char errbuf[128]; av_strerror(oret, errbuf, sizeof(errbuf));
            std::cerr << "open output failed: " << outUrl << " : " << errbuf << "\n"; return 1;
        }
        std::cerr << "[mux] output opened: " << outUrl << "\n";
    }
    {
        int hret = avformat_write_header(ofmt, nullptr);
        if (hret < 0) {
            char errbuf[128]; av_strerror(hret, errbuf, sizeof(errbuf));
            std::cerr << "write header failed: " << errbuf << "\n"; return 1;
        }
        std::cerr << "[mux] header written, streams=" << ofmt->nb_streams << "\n";
    }

    AVPacket* ipkt = av_packet_alloc();
    AVRational vtb = ifmt->streams[vIdx]->time_base;
    auto ms_to_tb = [&](int ms) -> int64_t {
        return av_rescale_q((int64_t)ms, AVRational{1, 1000}, vtb);
    };

    // Caption state
    const bool USE_ROLLUP = true;
    RollUpState ru_state{}; ru_state.lines = ru_lines;
    bool caption_pending = false;
    bool clear_pending = false;
    int clear_frames_left = 0; // repeat EDM on N packets so CC decoders latch
    bool auto_clear_sent = true;
    std::string current_caption;
    RollUpStyle current_style = default_style;
    std::vector<std::string> pending_lines;
    std::string prev_caption;
    std::string curr_caption;
    std::string last_caption;
    int64_t last_caption_expire_pts = AV_NOPTS_VALUE;
    bool bootstrap_pending = (bootstrap_enable != 0);
    std::string bootstrap_caption = "CC ONLINE";
    int64_t bootstrap_expire_pts = AV_NOPTS_VALUE;
    int64_t frame_count = 0;
    int cc_log = 0;
    // Hold last CC bytes so we can re-inject every N packets without re-rolling
    std::vector<uint8_t> linger_cc;
    // A/53 allows ≤31 cc triplets per SEI. Multi-row paints often exceed that —
    // queue and drain across video packets so VLC gets complete captions.
    std::vector<uint8_t> cc_tx_queue;
    std::deque<CapUdpEvt> cap_udp_pending;        // ordered caption events (FIFO)
    // H.264 B-frames arrive in decode order but decoders present CC in DISPLAY
    // order. A caption split over consecutive packets therefore arrives scrambled
    // on TVs (tail chunk shown before its head → "offering" ends as "offer").
    // Carry CC only on frames whose PTS is >= every PTS seen so far (I/P anchors).
    int64_t cc_carrier_pts = AV_NOPTS_VALUE;
    std::deque<std::vector<std::string>> pending_windows; // extra rows of a long "a|b|c|d" payload
    int live_paint_len = 0;
    int live_line_count = 0;
    std::vector<std::string> current_lines; // full window for live multi-line (Enter)
    std::vector<std::string> live_prev_lines; // last painted window (for CR vs overwrite)

    CaptionInput caprx{};
    if (use_external_udp_captions) {
        if (!open_udp_listener(caprx, cc_host, cc_port)) {
            std::cerr << "Failed to open UDP caption listener; continuing without external captions.\n";
            use_external_udp_captions = false;
        }
    }

    while (av_read_frame(ifmt, ipkt) >= 0) {
        if (ipkt->stream_index == vIdx) {
            ++frame_count;
            int64_t pts = (ipkt->pts != AV_NOPTS_VALUE) ? ipkt->pts :
                          (ipkt->dts != AV_NOPTS_VALUE) ? ipkt->dts : AV_NOPTS_VALUE;

            // Is this packet a safe CC carrier (display order == decode order)?
            bool cc_carrier = true;
            if (pts != AV_NOPTS_VALUE) {
                if (cc_carrier_pts != AV_NOPTS_VALUE) {
                    if (pts + ms_to_tb(10000) < cc_carrier_pts)
                        cc_carrier_pts = AV_NOPTS_VALUE;      // PTS wrap/discontinuity → resync
                    else if (pts < cc_carrier_pts)
                        cc_carrier = false;                   // B-frame: shown before an earlier carrier
                }
                if (cc_carrier) cc_carrier_pts = pts;
            }

            // Poll UDP — append every datagram in order (no "latest only" coalesce).
            if (use_external_udp_captions && caprx.enabled) {
                udp_drain_events(caprx.fd, cap_udp_pending, quiet);
            }

            // Apply ONE caption event per video frame, and only once the previous
            // paint fully left the SEI queue. This is what keeps word endings:
            // a CLEAR or new text that used to arrive mid-drain wiped the tail
            // ("offering" → "offer", "the" → "th").
            const bool cc_idle = cc_tx_queue.empty() && !clear_pending && clear_frames_left == 0;
            if (cc_idle && !caption_pending && pending_lines.empty() && !pending_windows.empty()) {
                // Next window of an over-long explicit payload ("a|b|c|d" with rows=2).
                current_lines = pending_windows.front();
                pending_windows.pop_front();
                current_caption = current_lines.back();
                caption_pending = true;
                last_caption = join_caption_lines(current_lines);
                last_caption_expire_pts = (pts == AV_NOPTS_VALUE) ? ms_to_tb(linger_ms)
                                        : pts + ms_to_tb(linger_ms);
                auto_clear_sent = (linger_ms <= 0);
            } else if (use_external_udp_captions && caprx.enabled && cc_idle &&
                       !cap_udp_pending.empty()) {
                const CapUdpEvt ev = cap_udp_pending.front();
                cap_udp_pending.pop_front();
                if (!quiet && cap_udp_pending.size() > 16 && (cc_log % 60) == 0)
                    std::cerr << "[cc] udp pending=" << cap_udp_pending.size() << " (ordered)\n";
                {
                    const std::string& latest = ev.line;
                    const bool want_clear = ev.is_clear;
                    if (want_clear) {
                        clear_pending = true;
                        caption_pending = false;
                        pending_lines.clear();
                        pending_windows.clear();
                        current_caption.clear();
                        current_lines.clear();
                        live_prev_lines.clear();
                    } else if (!latest.empty()) {
                        const bool explicit_window = (latest.find('|') != std::string::npos);
                        // '|': explicit window rows. Plain text: wrap ALL lines, then roll.
                        std::vector<std::string> got = split_caption_lines(latest, /*max_lines=*/0, max_cols);
                        if (!got.empty()) {
                            current_style = default_style;
                            for (auto& ln : got)
                                ln = strip_inline_style(ln, current_style);
                            if (explicit_window) {
                                // Multi-line (Enter / "|"): paint whole window at once.
                                // More rows than the window holds → queue the rest as
                                // further windows instead of dropping the first rows.
                                pending_windows.clear();
                                if ((int)got.size() > ru_lines) {
                                    for (size_t i = (size_t)ru_lines; i < got.size(); i += (size_t)ru_lines) {
                                        size_t e = std::min(got.size(), i + (size_t)ru_lines);
                                        pending_windows.emplace_back(got.begin() + (std::ptrdiff_t)i,
                                                                     got.begin() + (std::ptrdiff_t)e);
                                    }
                                    got.resize((size_t)ru_lines);
                                }
                                current_lines = got;
                                pending_lines.clear();
                                // Never store '|' in current_caption — that string may be painted
                                current_caption = current_lines.back().empty() && current_lines.size() >= 2
                                    ? current_lines.front()
                                    : current_lines.back();
                                caption_pending = true;
                                // Do NOT abort an in-progress CLEAR — leftover glyphs appear
                                // when the next paint arrives before EDM/DER finishes.
                                clear_pending = false;
                                last_caption = join_caption_lines(current_lines);
                                last_caption_expire_pts = (pts == AV_NOPTS_VALUE) ? ms_to_tb(linger_ms)
                                                        : pts + ms_to_tb(linger_ms);
                                auto_clear_sent = (linger_ms <= 0);
                            } else {
                                // One UDP line (maybe long): word-wrap → roll each full-word line
                                current_lines.clear();
                                pending_lines = got;
                                current_caption = pending_lines.front();
                                pending_lines.erase(pending_lines.begin());
                                caption_pending = true;
                                clear_pending = false;
                                int linger_use = linger_ms + (int)pending_lines.size() * 400;
                                last_caption = current_caption;
                                last_caption_expire_pts = (pts == AV_NOPTS_VALUE) ? ms_to_tb(linger_use)
                                                        : pts + ms_to_tb(linger_use);
                                auto_clear_sent = (linger_ms <= 0);
                            }
                        }
                    }
                }
            }

            // Roll the next wrapped line only after the previous one fully drained.
            if (!caption_pending && !pending_lines.empty() && cc_tx_queue.empty() &&
                (frame_count % line_gap) == 0) {
                current_caption = pending_lines.front();
                pending_lines.erase(pending_lines.begin());
                current_style = default_style;
                current_caption = strip_inline_style(current_caption, current_style);
                current_lines.clear();
                caption_pending = true;
                last_caption = current_caption;
                last_caption_expire_pts = (pts == AV_NOPTS_VALUE) ? ms_to_tb(linger_ms)
                                        : pts + ms_to_tb(linger_ms);
                auto_clear_sent = (linger_ms <= 0);
            }

            if (bootstrap_pending) {
                bootstrap_expire_pts = (pts == AV_NOPTS_VALUE) ? ms_to_tb(1000) : pts + ms_to_tb(1000);
                bootstrap_pending = false;
                last_caption = bootstrap_caption;
                last_caption_expire_pts = bootstrap_expire_pts;
                current_caption = bootstrap_caption;
                caption_pending = true;
            } else if (bootstrap_enable && pts != AV_NOPTS_VALUE &&
                       pts < bootstrap_expire_pts && !caption_pending) {
                current_caption = bootstrap_caption;
                caption_pending = true;
            }

            std::vector<uint8_t> cc;
            bool do_inject = false, do_repaint = false, do_roll = false, do_clear = false, do_overwrite = false;

            // Treat growing/shrinking same line as live edit (avoids RU2 duplicate rows while typing)
            auto is_live_edit = [](const std::string& prev, const std::string& next) -> bool {
                if (prev.empty() || next.empty()) return false;
                if (prev == next) return true;
                if (next.size() >= prev.size() && next.compare(0, prev.size(), prev) == 0) return true;
                if (prev.size() >= next.size() && prev.compare(0, next.size(), next) == 0) return true;
                return false;
            };

            if (clear_pending || clear_frames_left > 0) {
                if (clear_pending) {
                    clear_pending = false;
                    clear_frames_left = clear_frames;
                    if (!quiet)
                        std::cerr << "[cc] CLEAR start (repeat " << clear_frames_left << " pkts)\n";
                }
                do_clear = true; do_inject = true;
                // Count only packets that really carry the erase (anchor frames).
                if (clear_frames_left > 0 && cc_carrier) --clear_frames_left;
                prev_caption.clear(); curr_caption.clear(); current_caption.clear();
                current_lines.clear();
                live_prev_lines.clear();
                pending_lines.clear(); linger_cc.clear(); cc_tx_queue.clear();
                g_sei_cache.cc.clear(); g_sei_cache.sei.clear();
                last_caption_expire_pts = AV_NOPTS_VALUE;
                ru_state.started = false;
                live_paint_len = 0;
                live_line_count = 0;
                auto_clear_sent = true;
                caption_pending = false;
            } else if (caption_pending && (!current_caption.empty() || !current_lines.empty()) &&
                       !cc_tx_queue.empty()) {
                // Previous SEI chunks still draining — paint on a later frame so the
                // tail of the previous caption reaches the decoder first.
            } else if (caption_pending && (!current_caption.empty() || !current_lines.empty())) {
                caption_pending = false;
                auto_clear_sent = (linger_ms <= 0);
                const bool multi = current_lines.size() > 1;
                // Compare window state without embedding '|' into paint strings
                const std::string prev_key = live_prev_lines.empty()
                    ? curr_caption : join_caption_lines(live_prev_lines);
                const std::string next_key = current_lines.empty()
                    ? current_caption : join_caption_lines(current_lines);
                // Same text again while prior SEI chunks still draining → do NOT
                // rebuild. Clearing the queue dropped the last char pairs (VLC
                // showed "offer" while web had "offering").
                if (next_key == prev_key && !prev_key.empty()) {
                    // keep draining cc_tx_queue only
                } else {
                const bool edit = live_mode || multi || is_live_edit(prev_key, next_key)
                    || (!ru_state.started && curr_caption.empty() && live_prev_lines.empty());
                if (edit || multi) {
                    // Keep curr_caption free of '|' (paint-safe)
                    curr_caption = current_lines.empty() ? current_caption
                        : (current_lines.back().empty() && current_lines.size() >= 2
                           ? current_lines.front() : current_lines.back());
                    do_overwrite = true; do_inject = true;
                } else if (current_caption != curr_caption) {
                    prev_caption = curr_caption; curr_caption = current_caption;
                    do_roll = true; do_inject = true;
                } else {
                    do_repaint = true; do_inject = true;
                }
                }
            } else if (!auto_clear_sent && linger_ms > 0 &&
                       (!curr_caption.empty() || !live_prev_lines.empty()) &&
                       pts != AV_NOPTS_VALUE && last_caption_expire_pts != AV_NOPTS_VALUE &&
                       pts >= last_caption_expire_pts) {
                clear_frames_left = clear_frames;
                do_clear = true; do_inject = true;
                prev_caption.clear(); curr_caption.clear(); current_caption.clear();
                current_lines.clear();
                live_prev_lines.clear();
                last_caption.clear();
                pending_lines.clear(); linger_cc.clear(); cc_tx_queue.clear();
                last_caption_expire_pts = AV_NOPTS_VALUE;
                ru_state.started = false; auto_clear_sent = true;
                live_paint_len = 0;
                live_line_count = 0;
                caption_pending = false;
                if (!quiet) std::cerr << "[cc] timeout clear\n";
            }

            bool cc_changed = false;
            if (do_inject) {
                std::vector<uint8_t> cc608, cc708;
                if (do_clear) {
                    if (cc_mode & CC_MODE_608)
                        build_cc_clear(cc608, (uint8_t)base_row, ru_lines);
                    if (cc_mode & CC_MODE_708) build_708_caption_cc(cc708, {}, pen708, true);
                } else if (USE_ROLLUP) {
                    std::vector<std::string> lines708;
                    if (do_overwrite) {
                        if (current_lines.size() > 1) {
                            if (cc_mode & CC_MODE_608)
                                build_popon_window(cc608, current_lines, (uint8_t)base_row,
                                                   current_style, ru_lines);
                            ru_state.started = false;
                            live_paint_len = (int)current_lines.back().size();
                            live_prev_lines = current_lines;
                            live_line_count = (int)current_lines.size();
                            lines708 = current_lines;
                        } else {
                            if (cc_mode & CC_MODE_608)
                                build_ru_overwrite_cc(cc608, ru_state, current_caption, (uint8_t)base_row, current_style, live_paint_len, true);
                            live_prev_lines = std::vector<std::string>{current_caption};
                            live_line_count = 1;
                            lines708 = {current_caption};
                        }
                    } else if (do_roll) {
                        if (cc_mode & CC_MODE_608)
                            build_ru_update_cc(cc608, ru_state, current_caption, (uint8_t)base_row, current_style);
                        lines708 = {current_caption};
                    } else {
                        if (cc_mode & CC_MODE_608)
                            build_ru_repaint_no_roll(cc608, ru_state, current_caption, (uint8_t)base_row, current_style);
                        lines708 = {current_caption};
                    }
                    if (cc_mode & CC_MODE_708) {
                        pen708.color608 = current_style.color;
                        pen708.underline = current_style.underline;
                        pen708.italics = current_style.italics;
                        if (lines708.size() <= 1 || cc_mode == CC_MODE_708)
                            build_708_caption_cc(cc708, lines708, pen708, false);
                    }
                } else {
                    if (cc_mode & CC_MODE_608) build_popon_cc(cc608, current_caption);
                    if (cc_mode & CC_MODE_708) build_708_caption_cc(cc708, {current_caption}, pen708, false);
                }
                (void)do_repaint; (void)live_line_count;
                // Prefer complete 608 then 708 in a queue (drained ≤31 triplets/SEI).
                // Never replace the queue with an empty rebuild — that dropped the
                // undrained tail (last syllables missing in VLC).
                std::vector<uint8_t> built;
                if (cc_mode == CC_MODE_608) {
                    built = cc608;
                } else if (cc_mode == CC_MODE_708) {
                    built = cc708;
                } else {
                    built = cc608;
                    if (!cc708.empty())
                        built.insert(built.end(), cc708.begin(), cc708.end());
                }
                if (built.size() % 3)
                    built.resize(built.size() - (built.size() % 3));

                if (do_clear) {
                    cc_tx_queue = std::move(built);
                    linger_cc.clear();
                    g_sei_cache.cc.clear(); g_sei_cache.sei.clear();
                } else if (!built.empty()) {
                    cc_tx_queue = std::move(built);
                    build_cc_null_hold(linger_cc, 3);
                } else {
                    // No new pairs (unchanged rows) — keep draining prior queue.
                    build_cc_null_hold(linger_cc, 3);
                }
                if (!quiet && (do_clear || do_roll || do_overwrite || cc_log < 4 || (cc_log % 200) == 0)) {
                    std::cerr << "[cc] queue len=" << cc_tx_queue.size()
                              << (do_clear ? " (clear)" : (do_overwrite ? " (live)" : (do_roll ? " (roll)" : " (repaint)")))
                              << " pts=" << pts << "\n";
                }
                ++cc_log;
            }

            // Drain CC queue into this packet (A/53 max 31 triplets) — anchors only.
            if (cc_carrier && !cc_tx_queue.empty()) {
                const size_t maxb = 31 * 3;
                cc.clear();
                if (triplet_at_is_printable_text(cc_tx_queue, 0))
                    build_608_row_resume_preamble(cc, g_cc_resume_row, g_cc_resume_style);
                size_t room = maxb;
                if (cc.size() >= room)
                    room = 0;
                else {
                    room -= cc.size();
                    room -= room % 3;
                }
                if (room >= 3) {
                    size_t take = std::min(room, cc_tx_queue.size());
                    take -= take % 3;
                    if (take < cc_tx_queue.size())
                        take = align_cc_take_to_row_end(cc_tx_queue, take);
                    if (take > 0) {
                        cc.insert(cc.end(), cc_tx_queue.begin(),
                                  cc_tx_queue.begin() + (std::ptrdiff_t)take);
                        cc_tx_queue.erase(cc_tx_queue.begin(),
                                          cc_tx_queue.begin() + (std::ptrdiff_t)take);
                        cc_changed = true;
                        if (!quiet && (cc_log < 8 || !cc_tx_queue.empty()))
                            std::cerr << "[cc] inject len=" << cc.size()
                                      << " remain=" << cc_tx_queue.size() << "\n";
                    }
                }
            } else if (cc_carrier && !do_inject && !linger_cc.empty() && (frame_count % cc_every) == 0 &&
                       pts != AV_NOPTS_VALUE && last_caption_expire_pts != AV_NOPTS_VALUE &&
                       pts < last_caption_expire_pts) {
                // Hold last CC; sei_every throttles identical re-inserts
                cc = linger_cc;
            }

            AVPacket* outpkt = nullptr;
            const bool should_sei = cc_carrier && !cc.empty() &&
                (cc_changed || do_clear || (sei_every <= 1) || ((frame_count % sei_every) == 0));
            if (should_sei) outpkt = packet_with_a53_sei(ipkt, cc);
            if (!outpkt) outpkt = av_packet_clone(ipkt);
            if (outpkt) {
                av_packet_rescale_ts(outpkt, vtb, vout->time_base);
                outpkt->stream_index = vout->index;
                static uint64_t out_pkts = 0, out_bytes = 0;
                out_bytes += outpkt->size; ++out_pkts;
                if (!quiet && (out_pkts <= 3 || (out_pkts % 2000) == 0))
                    std::cerr << "[mux] video copy pkts=" << out_pkts << " bytes=" << out_bytes
                              << " last=" << outpkt->size << "\n";
                int wret = av_write_frame(ofmt, outpkt);
                if (wret < 0) {
                    char errbuf[128]; av_strerror(wret, errbuf, sizeof(errbuf));
                    std::cerr << "[mux] write failed: " << errbuf << "\n";
                }
                av_packet_free(&outpkt);
            }
        } else if (aIdx >= 0 && ipkt->stream_index == aIdx && aout) {
            AVPacket* ap = av_packet_clone(ipkt);
            if (ap) {
                av_packet_rescale_ts(ap, ifmt->streams[aIdx]->time_base, aout->time_base);
                ap->stream_index = aout->index;
                av_write_frame(ofmt, ap);
                av_packet_free(&ap);
            }
        }
        av_packet_unref(ipkt);
    }

    av_write_trailer(ofmt);
    av_packet_free(&ipkt);
    if (!(ofmt->oformat->flags & AVFMT_NOFILE)) avio_closep(&ofmt->pb);
    avformat_free_context(ofmt);
    avformat_close_input(&ifmt);
    if (caprx.fd >= 0) close(caprx.fd);

    std::cout << "Done: " << outUrl << "\n";
    return 0;
}

