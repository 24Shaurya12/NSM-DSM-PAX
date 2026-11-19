#include "layout_factory.h"

#include <map>
#include <stdexcept>

#include "util.h"

namespace {

std::map<std::string, LayoutBuilder>& registry() {
    static std::map<std::string, LayoutBuilder> r;
    return r;
}

}  // namespace

LayoutSpec parse_layout_spec(const std::string& text) {
    std::vector<std::string> parts = split(text, ':');
    if (parts.empty() || parts[0].empty()) throw std::invalid_argument("empty layout name");
    LayoutSpec spec;
    spec.name = parts[0];
    for (std::size_t i = 1; i < parts.size(); ++i) {
        std::size_t eq = parts[i].find('=');
        if (eq == std::string::npos || eq == 0)
            throw std::invalid_argument("expected key=value in layout '" + text + "'");
        spec.params[parts[i].substr(0, eq)] = parts[i].substr(eq + 1);
    }
    return spec;
}

void register_layout(const std::string& name, LayoutBuilder builder) {
    registry()[name] = std::move(builder);
}

std::vector<std::string> registered_layouts() {
    std::vector<std::string> names;
    for (const auto& [n, b] : registry()) names.push_back(n);
    return names;
}

std::unique_ptr<Layout> make_layout(const LayoutSpec& spec, const Table& table) {
    auto it = registry().find(spec.name);
    if (it == registry().end()) {
        std::string known;
        for (const std::string& n : registered_layouts()) known += (known.empty() ? "" : ", ") + n;
        throw std::invalid_argument("unknown layout '" + spec.name + "' (known: " + known + ")");
    }
    return it->second(table, spec.params);
}

void check_layout_params(const std::string& layout, const LayoutParams& params,
                         std::initializer_list<const char*> allowed) {
    for (const auto& [k, v] : params) {
        bool ok = false;
        for (const char* a : allowed) ok = ok || k == a;
        if (!ok) throw std::invalid_argument("layout '" + layout + "' has no parameter '" + k + "'");
    }
}

std::size_t layout_param_size(const LayoutParams& params, const std::string& key,
                              std::size_t def) {
    auto it = params.find(key);
    return it == params.end() ? def : parse_size(it->second);
}
