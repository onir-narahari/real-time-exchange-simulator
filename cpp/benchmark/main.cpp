/// C++ benchmark (Phase 7) — the frozen baseline structure, on identical flow.
///
/// Replays op streams emitted by the Python oracle:
///
///     python -m benchmarks.emit_ops --ops 200000 --seed 42 --out ops/
///     orderbook_benchmark --ops-dir ops --out benchmarks/results/baseline_cpp.json
///
/// Why recorded streams instead of a ported RNG: the workloads are pure
/// functions of (count, seed) built on random.Random — CPython seeding and
/// randbelow/uniform details that are easy to re-implement *almost* right.
/// Recording the stream guarantees both languages are timed on identical
/// flow. Equivalence is verified by counters: trades, volume, notional,
/// rested orders, cancel hits, and the final book must match the Python
/// baseline exactly (and do).
///
/// What is timed (mirrors benchmarks/benchmark.py):
/// - each operation individually, steady_clock nanoseconds around ONLY the
///   engine call (submit / cancel) — op loading and per-pass validate() are
///   outside the clock;
/// - pass runtime = wall time of the op loop; throughput = ops / runtime,
///   reported as the median across passes with the observed spread;
/// - latency percentiles pool every sample from every measured pass,
///   nearest-rank, same ranks as Python (rint = Python's round-half-even).
///
/// Frozen baseline structure (matches benchmarks/benchmark.py): all five
/// workloads in baseline order, 200,000 ops, seed 42, 5 measured passes,
/// 2 discarded warmups. The C++ numbers carry no GC flag — there is no
/// collector; the Python baseline ran with GC enabled.

#include <algorithm>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <numeric>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "orderbook/matching_engine.hpp"

namespace {

using namespace orderbook;
using Clock = std::chrono::steady_clock;

const char* const BASELINE_WORKLOADS[] = {
    "balanced", "cancel_heavy", "deep_book", "aggressive", "hot_price",
};

// ---------------------------------------------------------------------------
// Op streams
// ---------------------------------------------------------------------------

struct BenchOp {
    bool is_submit;
    Order order;          // valid when is_submit
    std::int64_t cancel_id = 0;
};

std::vector<BenchOp> load_ops(const std::string& path, std::int64_t expected_ops) {
    std::ifstream in(path);
    if (!in) {
        std::fprintf(stderr, "cannot open %s — generate it with "
                     "`python -m benchmarks.emit_ops`\n", path.c_str());
        std::exit(2);
    }
    std::vector<BenchOp> ops;
    ops.reserve(static_cast<std::size_t>(expected_ops));
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream tokens(line);
        char kind;
        tokens >> kind;
        if (kind == 'S') {
            std::int64_t id, quantity;
            std::string side, type, price_text;
            if (!(tokens >> id >> side >> type >> quantity >> price_text)) {
                std::fprintf(stderr, "%s: malformed submit: %s\n",
                             path.c_str(), line.c_str());
                std::exit(2);
            }
            std::optional<double> price;
            if (price_text != "-") price = std::stod(price_text) / 100.0;
            ops.push_back(BenchOp{true,
                                  Order(id, side_from_string(side),
                                        order_type_from_string(type),
                                        static_cast<int>(quantity), price),
                                  0});
        } else if (kind == 'C') {
            std::int64_t id;
            if (!(tokens >> id)) {
                std::fprintf(stderr, "%s: malformed cancel: %s\n",
                             path.c_str(), line.c_str());
                std::exit(2);
            }
            BenchOp op{false, Order(0, Side::BUY, OrderType::MARKET, 1), id};
            ops.push_back(op);
        } else {
            std::fprintf(stderr, "%s: unknown op: %s\n", path.c_str(), line.c_str());
            std::exit(2);
        }
    }
    if (static_cast<std::int64_t>(ops.size()) != expected_ops) {
        std::fprintf(stderr, "%s: %zu ops, expected %" PRId64 "\n",
                     path.c_str(), ops.size(), expected_ops);
        std::exit(2);
    }
    return ops;
}

// ---------------------------------------------------------------------------
// A single pass
// ---------------------------------------------------------------------------

struct Counters {
    long submits = 0;
    long market_orders = 0;
    long orders_rested = 0;
    long cancel_attempts = 0;
    long cancel_hits = 0;
    long trades = 0;
    long volume = 0;
    double notional = 0.0;
};

struct Pass {
    double runtime_sec = 0.0;
    double throughput = 0.0;
    std::vector<double> submit_us;
    std::vector<double> cancel_us;
    Counters counters;
    long resting_orders = 0;
    long bid_levels = 0;
    long ask_levels = 0;
    long bid_depth = 0;
    long ask_depth = 0;
    std::int64_t best_bid_ticks = 0;
    std::int64_t best_ask_ticks = 0;
    bool has_bid = false;
    bool has_ask = false;
    long levels_created = 0;
    long levels_removed = 0;
};

double now_sec() {
    return std::chrono::duration<double>(Clock::now().time_since_epoch()).count();
}

Pass run_pass(const std::vector<BenchOp>& ops) {
    OrderBook book;
    MatchingEngine engine;
    Pass pass;
    pass.submit_us.reserve(ops.size());
    pass.cancel_us.reserve(ops.size() / 2);

    const double started = now_sec();
    for (const BenchOp& op : ops) {
        if (op.is_submit) {
            const double begin = now_sec();
            MatchResult result = engine.submit(op.order, book);
            pass.submit_us.push_back((now_sec() - begin) * 1e6);

            ++pass.counters.submits;
            pass.counters.market_orders += op.order.is_limit() ? 0 : 1;
            pass.counters.orders_rested += result.resting ? 1 : 0;
            for (const Trade& trade : result.trades) {
                ++pass.counters.trades;
                pass.counters.volume += trade.quantity;
                pass.counters.notional += trade.notional();
            }
        } else {
            ++pass.counters.cancel_attempts;
            const double begin = now_sec();
            const bool hit = engine.cancel(op.cancel_id, book).has_value();
            pass.cancel_us.push_back((now_sec() - begin) * 1e6);
            pass.counters.cancel_hits += hit ? 1 : 0;
        }
    }
    pass.runtime_sec = now_sec() - started;
    pass.throughput = static_cast<double>(ops.size()) / pass.runtime_sec;

    book.validate();  // outside the clock, like the Python harness

    pass.resting_orders = static_cast<long>(book.size());
    pass.bid_levels = static_cast<long>(book.level_count(Side::BUY));
    pass.ask_levels = static_cast<long>(book.level_count(Side::SELL));
    pass.bid_depth = book.bid_depth();
    pass.ask_depth = book.ask_depth();
    if (auto bid = book.best_bid()) {
        pass.has_bid = true;
        pass.best_bid_ticks = static_cast<std::int64_t>(std::llround(*bid * 100.0));
    }
    if (auto ask = book.best_ask()) {
        pass.has_ask = true;
        pass.best_ask_ticks = static_cast<std::int64_t>(std::llround(*ask * 100.0));
    }
    pass.levels_created = book.level_creates();
    pass.levels_removed = book.level_removes();
    return pass;
}

// ---------------------------------------------------------------------------
// Statistics (same ranks as benchmarks/benchmark.py)
// ---------------------------------------------------------------------------

const double PERCENTILES[] = {50.0, 95.0, 99.0, 99.9};
constexpr std::size_t PERCENTILE_COUNT = 4;

double percentile(const std::vector<double>& sorted, double pct) {
    if (sorted.empty()) return 0.0;
    // Python: rank = max(1, min(n, int(round(pct/100 * n)))); sorted[rank-1].
    // Python's round() is half-to-even, which is exactly std::rint.
    const auto n = static_cast<double>(sorted.size());
    long rank = static_cast<long>(std::rint(pct / 100.0 * n));
    rank = std::max(1L, std::min(static_cast<long>(sorted.size()), rank));
    return sorted[static_cast<std::size_t>(rank - 1)];
}

struct Latency {
    long count = 0;
    double mean = 0.0;
    double min = 0.0;
    double max = 0.0;
    double p[PERCENTILE_COUNT] = {0.0, 0.0, 0.0, 0.0};
};

Latency summarize(std::vector<double>& samples) {
    Latency out;
    if (samples.empty()) return out;
    std::sort(samples.begin(), samples.end());
    out.count = static_cast<long>(samples.size());
    out.mean = std::accumulate(samples.begin(), samples.end(), 0.0) /
               static_cast<double>(out.count);
    out.min = samples.front();
    out.max = samples.back();
    for (std::size_t i = 0; i < PERCENTILE_COUNT; ++i)
        out.p[i] = percentile(samples, PERCENTILES[i]);
    return out;
}

double median(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const std::size_t mid = values.size() / 2;
    return values.size() % 2 ? values[mid]
                             : (values[mid - 1] + values[mid]) / 2.0;
}

// ---------------------------------------------------------------------------
// One workload: warmups + measured passes
// ---------------------------------------------------------------------------

struct Run {
    std::string workload;
    std::vector<double> throughputs;
    double throughput_median = 0.0;
    double spread_pct = 0.0;
    Latency submit;
    Latency cancel;
    Pass first;  // counters/final book are identical across passes
};

Run run_workload(const std::string& name, const std::vector<BenchOp>& ops,
                 int repeats, int warmup) {
    for (int i = 0; i < warmup; ++i) run_pass(ops);

    Run run;
    run.workload = name;
    std::vector<double> pooled_submit;
    std::vector<double> pooled_cancel;
    for (int i = 0; i < repeats; ++i) {
        Pass pass = run_pass(ops);
        if (i == 0) run.first = pass;
        run.throughputs.push_back(pass.throughput);
        pooled_submit.insert(pooled_submit.end(),
                             std::make_move_iterator(pass.submit_us.begin()),
                             std::make_move_iterator(pass.submit_us.end()));
        pooled_cancel.insert(pooled_cancel.end(),
                             std::make_move_iterator(pass.cancel_us.begin()),
                             std::make_move_iterator(pass.cancel_us.end()));
    }
    run.throughput_median = median(run.throughputs);
    const auto bounds =
        std::minmax_element(run.throughputs.begin(), run.throughputs.end());
    run.spread_pct =
        (*bounds.second - *bounds.first) / run.throughput_median * 100.0;
    run.submit = summarize(pooled_submit);
    run.cancel = summarize(pooled_cancel);
    return run;
}

// ---------------------------------------------------------------------------
// Reporting
// ---------------------------------------------------------------------------

std::string ticks_string(bool has, std::int64_t ticks) {
    if (!has) return "-";
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.2f", ticks / 100.0);
    return buf;
}

void print_table(const std::vector<Run>& runs, std::int64_t ops,
                 std::int64_t seed, int repeats, int warmup) {
    std::printf("\n==========================================================================\n");
    std::printf("C++ BASELINE\n");
    std::printf("==========================================================================\n");
    std::printf("  %" PRId64 " ops x %d repeats (%d warmup discarded), seed %" PRId64 "\n",
                ops, repeats, warmup, seed);
    std::printf("--------------------------------------------------------------------------\n");
    std::printf("%-14s%13s%8s%10s%8s%9s%9s%9s%9s%10s\n", "workload", "throughput",
                "spread", "trades", "op", "p50", "p95", "p99", "p99.9", "max");
    std::printf("--------------------------------------------------------------------------\n");
    for (const Run& run : runs) {
        for (int row = 0; row < 2; ++row) {
            const Latency& lat = row == 0 ? run.submit : run.cancel;
            if (!lat.count) continue;
            if (row == 0)
                std::printf("%-14s%10.0f/s%7.1f%%%10ld", run.workload.c_str(),
                            run.throughput_median, run.spread_pct,
                            run.first.counters.trades);
            else
                std::printf("%-14s%13s%8s%10s", "", "", "", "");
            std::printf("%8s%9.3f%9.3f%9.3f%9.3f%10.3f\n",
                        row == 0 ? "submit" : "cancel", lat.p[0], lat.p[1],
                        lat.p[2], lat.p[3], lat.max);
        }
    }
    std::printf("--------------------------------------------------------------------------\n");
    std::printf("  latencies in microseconds; throughput is the median of %d passes\n\n",
                repeats);
    for (const Run& run : runs) {
        const Pass& p = run.first;
        std::printf(
            "  %-14s resting %7ld  levels %4ldx%-4ld  depth %8ld/%-8ld  best %s/%s\n",
            run.workload.c_str(), p.resting_orders, p.bid_levels, p.ask_levels,
            p.bid_depth, p.ask_depth,
            ticks_string(p.has_bid, p.best_bid_ticks).c_str(),
            ticks_string(p.has_ask, p.best_ask_ticks).c_str());
    }
    std::printf("\n");
}

void write_json(const std::vector<Run>& runs, const std::string& path,
                std::int64_t ops, std::int64_t seed, int repeats, int warmup) {
    std::ofstream out(path);
    if (!out) {
        std::fprintf(stderr, "cannot write %s\n", path.c_str());
        return;
    }
    std::printf("wrote %s\n", path.c_str());

    char scratch[400];
    out << "{\n  \"kind\": \"baseline_cpp\",\n  \"config\": {"
        << "\"ops\": " << ops << ", \"seed\": " << seed
        << ", \"repeats\": " << repeats << ", \"warmup\": " << warmup
        << ", \"gc_disabled\": false, \"note\": \"no collector in C++\"},\n";

    char timestamp[32];
    std::time_t now = std::time(nullptr);
    std::strftime(timestamp, sizeof timestamp, "%Y-%m-%dT%H:%M:%S%z",
                  std::localtime(&now));
#if defined(__clang__)
    std::string compiler = std::string("clang ") + __clang_version__;
#elif defined(__GNUC__)
    std::string compiler = std::string("gcc ") + __VERSION__;
#else
    std::string compiler = "unknown";
#endif
#if defined(__aarch64__)
    const char* machine = "arm64";
#elif defined(__x86_64__)
    const char* machine = "x86_64";
#else
    const char* machine = "unknown";
#endif
    out << "  \"environment\": {\"language\": \"c++17\", \"compiler\": \""
        << compiler << "\", \"machine\": \"" << machine
        << "\", \"recorded_at\": \"" << timestamp << "\"},\n";

    out << "  \"runs\": [\n";
    for (std::size_t r = 0; r < runs.size(); ++r) {
        const Run& run = runs[r];
        const Pass& p = run.first;
        const Counters& c = p.counters;
        out << "    {\n";
        out << "      \"workload\": \"" << run.workload << "\",\n";
        out << "      \"throughput_ops_sec\": "
            << static_cast<long long>(std::llround(run.throughput_median));
        std::snprintf(scratch, sizeof scratch, "%.2f", run.spread_pct);
        out << ", \"throughput_spread_pct\": " << scratch
            << ",\n      \"throughput_per_pass\": [";
        for (std::size_t i = 0; i < run.throughputs.size(); ++i)
            out << (i ? ", " : "")
                << static_cast<long long>(std::llround(run.throughputs[i]));
        out << "],\n";
        out << "      \"operations\": {\"total\": " << ops
            << ", \"submits\": " << c.submits
            << ", \"market_orders\": " << c.market_orders
            << ", \"orders_rested\": " << c.orders_rested
            << ", \"cancel_attempts\": " << c.cancel_attempts
            << ", \"cancel_hits\": " << c.cancel_hits << "},\n";
        std::snprintf(scratch, sizeof scratch, "%.2f", c.notional);
        out << "      \"market\": {\"trades\": " << c.trades
            << ", \"volume\": " << c.volume << ", \"notional\": " << scratch
            << "},\n";
        const Latency* lats[] = {&run.submit, &run.cancel};
        const char* names[] = {"submit", "cancel"};
        out << "      \"latency\": {\n";
        for (int i = 0; i < 2; ++i) {
            // Emit both rows even when empty, matching the Python schema's
            // {"count": 0} for workloads that issue no cancels.
            std::snprintf(scratch, sizeof scratch,
                "\"count\": %ld, \"mean_us\": %.3f, \"min_us\": %.3f, "
                "\"max_us\": %.3f, \"p50_us\": %.3f, \"p95_us\": %.3f, "
                "\"p99_us\": %.3f, \"p99_9_us\": %.3f",
                lats[i]->count, lats[i]->mean, lats[i]->min, lats[i]->max,
                lats[i]->p[0], lats[i]->p[1], lats[i]->p[2], lats[i]->p[3]);
            out << "        \"" << names[i] << "\": {" << scratch << "}"
                << (i ? "" : ",");
            out << "\n";
        }
        out << "      },\n";
        out << "      \"final_book\": {\"resting_orders\": " << p.resting_orders
            << ", \"bid_levels\": " << p.bid_levels
            << ", \"ask_levels\": " << p.ask_levels
            << ", \"bid_depth\": " << p.bid_depth
            << ", \"ask_depth\": " << p.ask_depth
            << ", \"best_bid\": "
            << (p.has_bid ? ticks_string(true, p.best_bid_ticks) : "null")
            << ", \"best_ask\": "
            << (p.has_ask ? ticks_string(true, p.best_ask_ticks) : "null")
            << "},\n";
        out << "      \"level_churn\": {\"levels_created\": " << p.levels_created
            << ", \"levels_removed\": " << p.levels_removed << "}\n";
        out << "    }" << (r + 1 < runs.size() ? "," : "") << "\n";
    }
    out << "  ]\n}\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::string ops_dir = "ops";
    std::string out_path;
    std::string only_workload;
    std::int64_t ops = 200'000;
    std::int64_t seed = 42;
    int repeats = 5;
    int warmup = 2;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto value = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "%s needs a value\n", name);
                std::exit(2);
            }
            return argv[++i];
        };
        if (arg == "--ops-dir") ops_dir = value("--ops-dir");
        else if (arg == "--out") out_path = value("--out");
        else if (arg == "--workload") only_workload = value("--workload");
        else if (arg == "--ops") ops = std::stoll(value("--ops"));
        else if (arg == "--seed") seed = std::stoll(value("--seed"));
        else if (arg == "--repeats") repeats = std::stoi(value("--repeats"));
        else if (arg == "--warmup") warmup = std::stoi(value("--warmup"));
        else if (arg == "--help" || arg == "-h") {
            std::printf(
                "usage: orderbook_benchmark [--ops-dir DIR] [--workload NAME]\n"
                "       [--ops N] [--seed N] [--repeats N] [--warmup N] [--out FILE]\n"
                "defaults: the frozen baseline structure (200000 ops, seed 42,\n"
                "5 repeats, 2 warmup, all five workloads)\n");
            return 0;
        } else {
            std::fprintf(stderr, "unknown option %s (see --help)\n", arg.c_str());
            return 2;
        }
    }

    std::vector<Run> runs;
    for (const char* name : BASELINE_WORKLOADS) {
        if (!only_workload.empty() && only_workload != name) continue;
        const std::string path = ops_dir + "/" + name + ".ops";
        std::fprintf(stderr, "loading %s ... ", path.c_str());
        const std::vector<BenchOp> stream = load_ops(path, ops);
        std::fprintf(stderr, "%zu ops\n", stream.size());
        runs.push_back(run_workload(name, stream, repeats, warmup));
        std::fprintf(stderr, "  %-14s %9.0f ops/s median (spread %.1f%%)\n",
                     name, runs.back().throughput_median,
                     runs.back().spread_pct);
    }
    if (runs.empty()) {
        std::fprintf(stderr, "unknown workload %s\n", only_workload.c_str());
        return 2;
    }

    print_table(runs, ops, seed, repeats, warmup);
    if (!out_path.empty())
        write_json(runs, out_path, ops, seed, repeats, warmup);
    return 0;
}
