/// Test runner: the explicit price-time-priority suite (Phase 4).
///
/// Cross-language agreement with the Python oracle is proven separately by
/// the differential harness (Phases 5–6); these cases pin the semantics down
/// as readable, standalone C++.

#include <cstdio>
#include <vector>

#include "check.h"

std::vector<testing::Case> price_time_cases();
std::vector<testing::Case> matching_cases();
std::vector<testing::Case> cancellation_cases();

int main() {
    int failed = 0;
    int cases = 0;
    for (auto group : {price_time_cases(), matching_cases(), cancellation_cases()}) {
        cases += static_cast<int>(group.size());
        failed += testing::run_cases(group.data(), group.size());
    }
    std::printf("%d checks, %d cases, %d failed\n",
                testing::check_count(), cases, failed);
    return failed == 0 ? 0 : 1;
}
