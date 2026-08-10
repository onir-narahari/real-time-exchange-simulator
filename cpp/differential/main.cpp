/// Cross-language differential checker (Phases 5-6).
///
/// Replays vector files produced by the Python oracle:
///
///     python -m reference.vectors --seeds 100 --ops 1000 --out vectors/
///     orderbook_differential vectors/*.vec
///
/// Every operation is executed against the C++ engine and its outcome
/// compared against what Python recorded: trades (every field), the
/// incoming order's remaining/rested state, cancel hit/miss, top of book,
/// depths, resting count, and FNV-1a digests of the canonical L2/L3 book
/// text. Prices travel as integer ticks (price x 100) so no floating-point
/// formatting ever crosses the language boundary.
///
/// On the FIRST divergence the checker prints the case, the operation, both
/// outcomes, and the full C++ book, plus the command that prints the Python
/// book at the same operation:
///
///     python -m reference.vectors explain --profile <p> --seed <s> \
///         --ops <n> --stop-at <i>
///
/// Options: `--case <seed>` replays only that case (for reproduction).

#include <cinttypes>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "orderbook/matching_engine.hpp"

namespace {

using namespace orderbook;

// ---------------------------------------------------------------------------
// Canonical state digests — must match reference/vectors.py byte-for-byte.
// ---------------------------------------------------------------------------

std::uint64_t fnv1a(const std::string& text) {
    std::uint64_t h = 14695981039346656037ull;
    for (unsigned char c : text) {
        h ^= c;
        h *= 1099511628211ull;
    }
    return h;
}

std::int64_t ticks_of(double price) {
    return static_cast<std::int64_t>(std::llround(price * 100.0));
}

std::string hex64(std::uint64_t value) {
    char buf[17];
    std::snprintf(buf, sizeof buf, "%016" PRIx64, value);
    return buf;
}

std::string canonical_l3(const OrderBook& book) {
    std::string text;
    for (const L3Row& row : book.l3(Side::BUY)) {
        text += "b " + std::to_string(row.order_id) + " " +
                std::to_string(ticks_of(row.price)) + " " +
                std::to_string(row.remaining) + "\n";
    }
    for (const L3Row& row : book.l3(Side::SELL)) {
        text += "a " + std::to_string(row.order_id) + " " +
                std::to_string(ticks_of(row.price)) + " " +
                std::to_string(row.remaining) + "\n";
    }
    return text;
}

std::string canonical_l2(const OrderBook& book) {
    std::string text;
    BookSnapshot snapshot = book.l2();
    for (const LevelView& level : snapshot.bids) {
        text += "B " + std::to_string(ticks_of(level.price)) + " " +
                std::to_string(level.quantity) + " " +
                std::to_string(level.order_count) + "\n";
    }
    for (const LevelView& level : snapshot.asks) {
        text += "A " + std::to_string(ticks_of(level.price)) + " " +
                std::to_string(level.quantity) + " " +
                std::to_string(level.order_count) + "\n";
    }
    return text;
}

// ---------------------------------------------------------------------------
// Replay context and divergence reporting
// ---------------------------------------------------------------------------

struct Context {
    std::string profile;
    std::int64_t seed = 0;
    std::int64_t declared_ops = 0;
    std::string op_line;  // the op currently under test, verbatim
    std::int64_t op_index = -1;
};

std::string show_ticks(std::optional<double> price) {
    return price ? std::to_string(ticks_of(*price)) : std::string("-");
}

[[noreturn]] void diverge(const Context& ctx, const OrderBook& book,
                          const std::string& what, const std::string& expected,
                          const std::string& actual) {
    std::fprintf(stderr,
        "\n======================================================================\n"
        "DIVERGENCE: %s\n"
        "======================================================================\n"
        "  case      profile=%s seed=%" PRId64 " ops=%" PRId64 "\n"
        "  operation #%" PRId64 ": %s\n"
        "  expected  %s\n"
        "  actual    %s\n",
        what.c_str(), ctx.profile.c_str(), ctx.seed, ctx.declared_ops,
        ctx.op_index, ctx.op_line.c_str(), expected.c_str(), actual.c_str());

    std::fprintf(stderr, "  --- C++ book ---\n");
    std::fprintf(stderr, "    best_bid %s  best_ask %s  bid_depth %ld  ask_depth %ld"
                         "  resting %zu\n",
                 show_ticks(book.best_bid()).c_str(),
                 show_ticks(book.best_ask()).c_str(),
                 book.bid_depth(), book.ask_depth(), book.size());
    std::fprintf(stderr, "    l2 digest %s  l3 digest %s\n",
                 hex64(fnv1a(canonical_l2(book))).c_str(),
                 hex64(fnv1a(canonical_l3(book))).c_str());
    std::fprintf(stderr, "    bids L3:\n");
    for (const L3Row& row : book.l3(Side::BUY))
        std::fprintf(stderr, "      id=%" PRId64 " %" PRId64 " x %d\n",
                     row.order_id, ticks_of(row.price), row.remaining);
    std::fprintf(stderr, "    asks L3:\n");
    for (const L3Row& row : book.l3(Side::SELL))
        std::fprintf(stderr, "      id=%" PRId64 " %" PRId64 " x %d\n",
                     row.order_id, ticks_of(row.price), row.remaining);

    std::fprintf(stderr,
        "  reproduce:\n"
        "    python -m reference.vectors explain --profile %s --seed %" PRId64
        " --ops %" PRId64 " --stop-at %" PRId64 "\n"
        "    orderbook_differential vectors/%s.vec --case %" PRId64 "\n"
        "======================================================================\n",
        ctx.profile.c_str(), ctx.seed, ctx.declared_ops, ctx.op_index,
        ctx.profile.c_str(), ctx.seed);
    std::exit(1);
}

[[noreturn]] void malformed(const std::string& path, long line_no,
                            const std::string& line, const std::string& why) {
    std::fprintf(stderr, "malformed vector file %s:%ld: %s\n  line: %s\n",
                 path.c_str(), line_no, why.c_str(), line.c_str());
    std::exit(2);
}

// ---------------------------------------------------------------------------
// Vector file replay
// ---------------------------------------------------------------------------

struct ExpectedTrade {
    std::int64_t trade_id;
    std::int64_t price_ticks;
    int quantity;
    std::string aggressor_side;
    std::int64_t aggressor_order_id;
    std::int64_t resting_order_id;
};

struct Stats {
    long cases = 0;
    long ops = 0;
    long trades = 0;
};

// Reads the next non-empty line; false at EOF.
bool next_line(std::istream& in, std::string& line, long& line_no) {
    while (static_cast<bool>(std::getline(in, line))) {
        ++line_no;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (!line.empty() && line[0] != '#') return true;
    }
    return false;
}

bool replay_file(const std::string& path, std::optional<std::int64_t> only_seed,
                 Stats& stats) {
    std::ifstream in(path);
    if (!in) {
        std::fprintf(stderr, "cannot open %s\n", path.c_str());
        return false;
    }

    OrderBook book;
    MatchingEngine engine;
    Context ctx;
    std::string line;
    long line_no = 0;
    bool skipping = false;  // inside a case filtered out by --case

    while (next_line(in, line, line_no)) {
        std::istringstream tokens(line);
        std::string tag;
        tokens >> tag;

        if (tag == "case") {
            if (!(tokens >> ctx.profile >> ctx.seed >> ctx.declared_ops))
                malformed(path, line_no, line, "bad case header");
            skipping = only_seed.has_value() && ctx.seed != *only_seed;
            if (skipping) continue;
            book = OrderBook();
            engine = MatchingEngine();
            ++stats.cases;
            continue;
        }
        if (skipping) continue;

        if (tag != "op")
            malformed(path, line_no, line, "expected 'op' or 'case'");
        ctx.op_line = line;

        std::string kind;
        if (!(tokens >> ctx.op_index >> kind))
            malformed(path, line_no, line, "bad op line");

        // --- expected block -------------------------------------------------
        std::vector<ExpectedTrade> expected_trades;
        std::optional<std::pair<int, int>> expected_result;  // remaining, rested
        std::optional<int> expected_cancel;

        std::string block;
        if (kind == "submit") {
            if (!next_line(in, block, line_no))
                malformed(path, line_no, line, "truncated: expected 'ex' block");
            std::istringstream ex(block);
            std::string ex_tag;
            std::int64_t ex_index, trade_count;
            if (!(ex >> ex_tag >> ex_index >> tag >> trade_count) ||
                ex_tag != "ex" || tag != "trades" || ex_index != ctx.op_index)
                malformed(path, line_no, block, "bad 'ex' line");
            for (std::int64_t i = 0; i < trade_count; ++i) {
                if (!next_line(in, block, line_no))
                    malformed(path, line_no, line, "truncated: expected 'tr' line");
                std::istringstream tr(block);
                ExpectedTrade t;
                std::string tr_tag;
                if (!(tr >> tr_tag >> t.trade_id >> t.price_ticks >> t.quantity >>
                      t.aggressor_side >> t.aggressor_order_id >>
                      t.resting_order_id) || tr_tag != "tr")
                    malformed(path, line_no, block, "bad 'tr' line");
                expected_trades.push_back(t);
            }
            if (!next_line(in, block, line_no))
                malformed(path, line_no, line, "truncated: expected 'rs' line");
            std::istringstream rs(block);
            std::string rs_tag;
            std::int64_t rs_index;
            int remaining, rested;
            if (!(rs >> rs_tag >> rs_index >> remaining >> rested) ||
                rs_tag != "rs" || rs_index != ctx.op_index)
                malformed(path, line_no, block, "bad 'rs' line");
            expected_result = {remaining, rested};
        } else if (kind == "cancel") {
            if (!next_line(in, block, line_no))
                malformed(path, line_no, line, "truncated: expected 'cx' line");
            std::istringstream cx(block);
            std::string cx_tag;
            std::int64_t cx_index;
            int hit;
            if (!(cx >> cx_tag >> cx_index >> hit) || cx_tag != "cx" ||
                cx_index != ctx.op_index)
                malformed(path, line_no, block, "bad 'cx' line");
            expected_cancel = hit;
        } else {
            malformed(path, line_no, line, "unknown op kind");
        }

        // --- state line ------------------------------------------------------
        if (!next_line(in, block, line_no))
            malformed(path, line_no, line, "truncated: expected 'st' line");
        std::istringstream st(block);
        std::string st_tag, exp_best_bid, exp_best_ask, exp_l2, exp_l3;
        std::int64_t st_index, exp_bid_depth, exp_ask_depth, exp_resting;
        if (!(st >> st_tag >> st_index >> exp_best_bid >> exp_best_ask >>
              exp_bid_depth >> exp_ask_depth >> exp_resting >> exp_l2 >> exp_l3) ||
            st_tag != "st" || st_index != ctx.op_index)
            malformed(path, line_no, block, "bad 'st' line");

        // --- execute ----------------------------------------------------------
        if (kind == "submit") {
            std::int64_t order_id, quantity;
            std::string side_text, type_text, price_text;
            // re-parse the op tail (tokens stream already consumed kind)
            if (!(tokens >> order_id >> side_text >> type_text >> quantity >>
                  price_text))
                malformed(path, line_no, line, "bad submit op");
            std::optional<double> price;
            if (price_text != "-")
                price = std::stod(price_text) / 100.0;

            MatchResult result = engine.submit(
                Order(order_id, side_from_string(side_text),
                      order_type_from_string(type_text),
                      static_cast<int>(quantity), price),
                book);

            // trades: count, then every field of every trade
            if (static_cast<std::int64_t>(result.trades.size()) !=
                static_cast<std::int64_t>(expected_trades.size()))
                diverge(ctx, book, "trade count",
                        std::to_string(expected_trades.size()) + " trades",
                        std::to_string(result.trades.size()) + " trades");
            for (std::size_t i = 0; i < expected_trades.size(); ++i) {
                const ExpectedTrade& e = expected_trades[i];
                const Trade& a = result.trades[i];
                std::string actual =
                    std::to_string(a.trade_id) + " " +
                    std::to_string(ticks_of(a.price)) + " " +
                    std::to_string(a.quantity) + " " +
                    std::string(to_string(a.aggressor_side)) + " " +
                    std::to_string(a.aggressor_order_id) + " " +
                    std::to_string(a.resting_order_id);
                std::string expected =
                    std::to_string(e.trade_id) + " " +
                    std::to_string(e.price_ticks) + " " +
                    std::to_string(e.quantity) + " " + e.aggressor_side + " " +
                    std::to_string(e.aggressor_order_id) + " " +
                    std::to_string(e.resting_order_id);
                if (actual != expected)
                    diverge(ctx, book,
                            "trade #" + std::to_string(i) +
                                " (id ticks qty aggr_side aggr_id rest_id)",
                            expected, actual);
            }
            stats.trades += static_cast<long>(result.trades.size());

            if (result.order.remaining != expected_result->first ||
                static_cast<int>(result.resting) != expected_result->second)
                diverge(ctx, book, "result (remaining rested)",
                        std::to_string(expected_result->first) + " " +
                            std::to_string(expected_result->second),
                        std::to_string(result.order.remaining) + " " +
                            std::to_string(static_cast<int>(result.resting)));
        } else {
            std::int64_t order_id;
            if (!(tokens >> order_id))
                malformed(path, line_no, line, "bad cancel op");
            bool hit = engine.cancel(order_id, book).has_value();
            if (static_cast<int>(hit) != *expected_cancel)
                diverge(ctx, book, "cancel hit", std::to_string(*expected_cancel),
                        std::to_string(static_cast<int>(hit)));
        }
        ++stats.ops;

        // --- state ----------------------------------------------------------
        std::string actual_bid = show_ticks(book.best_bid());
        std::string actual_ask = show_ticks(book.best_ask());
        if (actual_bid != exp_best_bid || actual_ask != exp_best_ask ||
            book.bid_depth() != exp_bid_depth ||
            book.ask_depth() != exp_ask_depth ||
            static_cast<std::int64_t>(book.size()) != exp_resting)
            diverge(ctx, book,
                    "state (best_bid best_ask bid_depth ask_depth resting)",
                    exp_best_bid + " " + exp_best_ask + " " +
                        std::to_string(exp_bid_depth) + " " +
                        std::to_string(exp_ask_depth) + " " +
                        std::to_string(exp_resting),
                    actual_bid + " " + actual_ask + " " +
                        std::to_string(book.bid_depth()) + " " +
                        std::to_string(book.ask_depth()) + " " +
                        std::to_string(book.size()));

        std::string actual_l2 = hex64(fnv1a(canonical_l2(book)));
        if (actual_l2 != exp_l2)
            diverge(ctx, book, "L2 book digest", exp_l2, actual_l2);
        std::string actual_l3 = hex64(fnv1a(canonical_l3(book)));
        if (actual_l3 != exp_l3)
            diverge(ctx, book, "L3 book digest", exp_l3, actual_l3);

        book.validate();
    }
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    std::optional<std::int64_t> only_seed;
    std::vector<std::string> files;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--case" && i + 1 < argc) {
            only_seed = std::stoll(argv[++i]);
        } else if (arg == "--help" || arg == "-h") {
            std::printf("usage: orderbook_differential [--case <seed>] <file.vec>...\n");
            return 0;
        } else {
            files.push_back(arg);
        }
    }
    if (files.empty()) {
        std::fprintf(stderr, "no vector files given (see --help)\n");
        return 2;
    }

    Stats total;
    for (const std::string& file : files) {
        Stats stats;
        if (!replay_file(file, only_seed, stats)) return 2;
        total.cases += stats.cases;
        total.ops += stats.ops;
        total.trades += stats.trades;
        std::printf("  %-28s %5ld cases  %8ld ops  %8ld trades compared\n",
                    file.c_str(), stats.cases, stats.ops, stats.trades);
    }
    std::printf("%ld cases, %ld operations, %ld trades compared -- no divergence\n",
                total.cases, total.ops, total.trades);
    return 0;
}
