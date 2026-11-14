#pragma once

#include <sys/mman.h>

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <new>

constexpr std::size_t kCacheLineSize = 64;
constexpr std::size_t kHugePageSize = 2 * 1024 * 1024;

// How AlignedBuffer allocates. Set once from the command line, before anything is built.
//   hugepages  buffers of at least 2 MB are 2 MB aligned and madvise(MADV_HUGEPAGE)d before
//              they are first touched, so transparent huge pages back them (THP "madvise"
//              mode is enough; no root needed). Fewer, larger pages -> fewer dTLB misses.
struct AllocPolicy {
    bool hugepages = false;
};

inline AllocPolicy& alloc_policy() {
    static AllocPolicy policy;
    return policy;
}

// Owning, zero-initialised, 64-byte aligned array of T.
//
// Buffers of kMmapBytes or more get their own anonymous mmap, so they always start on a page
// boundary (a 2 MB boundary with hugepages). That makes the placement deterministic. With
// malloc it depends on allocator state: glibc raises its mmap threshold after a large free,
// so the same 32 MB DSM columns came from separate mmaps (all page-aligned) in one run and
// back to back from the heap (incidentally staggered across cache sets) in another, and early
// SELECT * on DSM took 63 ms or 21 ms depending on which (see the report).
//
// `offset_bytes` (a multiple of 64) shifts the start of the array that far past the aligned
// allocation. Page-aligned arrays read in lockstep (e.g. DSM columns during tuple
// reconstruction) map to the same cache sets; giving each array a different offset spreads
// them over different sets (--dsm-offset).
inline constexpr std::size_t kMmapBytes = 64 * 1024;

template <typename T>
class AlignedBuffer {
public:
    AlignedBuffer() = default;

    explicit AlignedBuffer(std::size_t count, std::size_t offset_bytes = 0) : size_(count) {
        std::size_t bytes = count * sizeof(T) + offset_bytes;
        if (bytes >= kMmapBytes) {
            const bool huge = alloc_policy().hugepages && bytes >= kHugePageSize;
            const std::size_t align = huge ? kHugePageSize : 4096;
            bytes = (bytes + align - 1) / align * align;
            // Over-allocate by one alignment unit and trim, so the start is `align`-aligned.
            const std::size_t span = bytes + (huge ? align : 0);
            void* m = mmap(nullptr, span, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (m == MAP_FAILED) throw std::bad_alloc();
            char* p = static_cast<char*>(m);
            if (huge) {
                char* a = reinterpret_cast<char*>(
                    (reinterpret_cast<std::uintptr_t>(p) + align - 1) / align * align);
                if (a > p) munmap(p, a - p);
                if (a + bytes < p + span) munmap(a + bytes, p + span - (a + bytes));
                p = a;
                madvise(p, bytes, MADV_HUGEPAGE);  // best effort
            }
            std::memset(p, 0, bytes);  // first touch: the pages are allocated here
            base_.reset(p);
            base_.get_deleter().bytes = bytes;
            data_ = reinterpret_cast<T*>(p + offset_bytes);
            return;
        }
        // std::aligned_alloc requires the byte size to be a multiple of the alignment.
        bytes = (bytes + kCacheLineSize - 1) / kCacheLineSize * kCacheLineSize;
        if (bytes == 0) bytes = kCacheLineSize;
        void* p = std::aligned_alloc(kCacheLineSize, bytes);
        if (p == nullptr) throw std::bad_alloc();
        std::memset(p, 0, bytes);
        base_.reset(static_cast<char*>(p));
        data_ = reinterpret_cast<T*>(static_cast<char*>(p) + offset_bytes);
    }

    T* data() { return data_; }
    const T* data() const { return data_; }
    std::size_t size() const { return size_; }

    T& operator[](std::size_t i) { return data_[i]; }
    const T& operator[](std::size_t i) const { return data_[i]; }

private:
    struct FreeDeleter {
        std::size_t bytes = 0;  // nonzero: the buffer is an mmap of this many bytes
        void operator()(char* p) const {
            if (bytes) munmap(p, bytes);
            else std::free(p);
        }
    };
    std::unique_ptr<char, FreeDeleter> base_;
    T* data_ = nullptr;
    std::size_t size_ = 0;
};
