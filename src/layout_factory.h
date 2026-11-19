#pragma once

#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "layout.h"
#include "table.h"

// A layout requested on the command line: "nsm", "dsm", "pax:page=256K", ...
// Parameters are ':'-separated key=value pairs after the name.
struct LayoutSpec {
    std::string name;
    LayoutParams params;
};

LayoutSpec parse_layout_spec(const std::string& text);

using LayoutBuilder =
    std::function<std::unique_ptr<Layout>(const Table& table, const LayoutParams& params)>;

// Registers a layout under `name`. Each layout does this from its own .cpp file.
void register_layout(const std::string& name, LayoutBuilder builder);
std::vector<std::string> registered_layouts();

// Builds a layout. Throws std::invalid_argument for unknown names or parameters.
std::unique_ptr<Layout> make_layout(const LayoutSpec& spec, const Table& table);

// Helpers for builders.
void check_layout_params(const std::string& layout, const LayoutParams& params,
                         std::initializer_list<const char*> allowed);
std::size_t layout_param_size(const LayoutParams& params, const std::string& key,
                              std::size_t def);
