#pragma once

#include <cstddef>

// How predicates are evaluated.
//   Branchy:    `if (match) keep` - cheap when the outcome is predictable, slow near 50%.
//   Branchless: always write the candidate and advance by the match bit - no mispredictions,
//               but every candidate costs a store.
enum class FilterMode { Branchy, Branchless };

inline const char* filter_mode_name(FilterMode m) {
    return m == FilterMode::Branchy ? "branchy" : "branchless";
}

// Execution choices that can differ per query, independent of how the table is stored.
// A layout ignores the options it has no use for (Layout::uses_batch_rows tells the driver).
struct ExecOptions {
    std::size_t dsm_batch = 1024;            // rows per DSM batch (selection vector length)
    FilterMode filter = FilterMode::Branchy;
    // Reserved for multithreaded execution. Only 1 is supported today; adding threads means
    // splitting rows/pages into per-thread ranges inside the layouts (see further_plan.md).
    int threads = 1;
};
