#pragma once

#include <cstdint>
#include <string>

class NsmLayout;
class DsmLayout;
class PaxLayout;

// A query has one run() overload per layout, so all of a query's layout-specific code
// lives together in its own file under src/queries/. Layouts only expose their storage.
//
// Anything a query needs besides the scan itself (e.g. allocating an output buffer)
// belongs in its constructor, which the driver calls outside the timed region.
class Query {
public:
    virtual ~Query() = default;

    virtual std::string name() const = 0;

    virtual void run(const NsmLayout& t) = 0;
    virtual void run(const DsmLayout& t) = 0;
    virtual void run(const PaxLayout& t) = 0;

    // Value summarising the last run's output; must be identical across layouts.
    // Called outside the timed region.
    virtual int64_t result() const = 0;
};
