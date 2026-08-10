/// Minimal assert-style test harness — no framework, by design.
///
/// A test is `bool fn()`: return false on the first failed CHECK. Cases are
/// plain arrays registered explicitly in main.cpp — no static initialization,
/// no macros beyond CHECK.

#pragma once

#include <cstddef>
#include <cstdio>

namespace testing {

struct Case {
    const char* name;
    bool (*fn)();
};

inline int& check_count() {
    static int n = 0;
    return n;
}

inline int run_cases(const Case* cases, std::size_t n) {
    int failed = 0;
    for (std::size_t i = 0; i < n; ++i) {
        if (!cases[i].fn()) {
            std::fprintf(stderr, "  ^^ in case %s\n", cases[i].name);
            ++failed;
        }
    }
    return failed;
}

}  // namespace testing

#define CHECK(cond)                                                          \
    do {                                                                     \
        ++::testing::check_count();                                          \
        if (!(cond)) {                                                       \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,     \
                         #cond);                                             \
            return false;                                                    \
        }                                                                    \
    } while (0)
