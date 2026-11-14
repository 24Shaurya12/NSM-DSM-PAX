#include "encodings/encoded_column.h"

#include <stdexcept>

namespace {

struct EncodingInfo {
    Encoding encoding;
    const char* name;
    std::unique_ptr<EncodedColumn> (*make)(const std::vector<int64_t>&);
};

// To add an encoding: write src/encodings/<name>.cpp and add one entry here.
const EncodingInfo kEncodings[] = {
    {Encoding::Plain, "plain", make_plain_column},
    {Encoding::Bitpack, "bitpack", make_bitpack_column},
    {Encoding::For, "for", make_for_column},
    {Encoding::Dict, "dict", make_dict_column},
    {Encoding::Rle, "rle", make_rle_column},
    {Encoding::Delta, "delta", make_delta_column},
};

const EncodingInfo& info(Encoding e) {
    for (const auto& i : kEncodings)
        if (i.encoding == e) return i;
    throw std::logic_error("unregistered encoding");
}

}  // namespace

const std::vector<Encoding>& all_encodings() {
    static const std::vector<Encoding> all = [] {
        std::vector<Encoding> v;
        for (const auto& i : kEncodings) v.push_back(i.encoding);
        return v;
    }();
    return all;
}

std::string encoding_name(Encoding e) { return info(e).name; }

bool parse_encoding(const std::string& name, Encoding& out) {
    for (const auto& i : kEncodings) {
        if (name == i.name) {
            out = i.encoding;
            return true;
        }
    }
    return false;
}

std::unique_ptr<EncodedColumn> encode_column(Encoding e, const std::vector<int64_t>& values) {
    return info(e).make(values);
}
