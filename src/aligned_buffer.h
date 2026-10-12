#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <new>

constexpr std::size_t kCacheLineSize = 64;

// Owning, zero-initialised, 64-byte aligned array of T.
template <typename T>
class AlignedBuffer {
public:
    AlignedBuffer() = default;

    explicit AlignedBuffer(std::size_t count) : size_(count) {
        // std::aligned_alloc requires the byte size to be a multiple of the alignment.
        std::size_t bytes = count * sizeof(T);
        bytes = (bytes + kCacheLineSize - 1) / kCacheLineSize * kCacheLineSize;
        if (bytes == 0) bytes = kCacheLineSize;
        void* p = std::aligned_alloc(kCacheLineSize, bytes);
        if (p == nullptr) throw std::bad_alloc();
        std::memset(p, 0, bytes);
        data_.reset(static_cast<T*>(p));
    }

    T* data() { return data_.get(); }
    const T* data() const { return data_.get(); }
    std::size_t size() const { return size_; }

    T& operator[](std::size_t i) { return data_.get()[i]; }
    const T& operator[](std::size_t i) const { return data_.get()[i]; }

private:
    struct FreeDeleter {
        void operator()(T* p) const { std::free(p); }
    };
    std::unique_ptr<T, FreeDeleter> data_;
    std::size_t size_ = 0;
};
