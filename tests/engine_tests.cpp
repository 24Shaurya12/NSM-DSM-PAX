// Engine test suite: every layout x operator x execution option, on awkward table shapes and
// data distributions, compared against the reference executor; plus parser tests.
// Run with `make test`.

#include <exception>
#include <functional>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "distributions.h"
#include "exec_options.h"
#include "layout_factory.h"
#include "query_spec.h"
#include "reference.h"
#include "table.h"

namespace {

int g_checks = 0;
int g_failures = 0;

void check(bool ok, const std::string& what) {
    ++g_checks;
    if (!ok) {
        ++g_failures;
        if (g_failures <= 20) std::cerr << "FAIL: " << what << "\n";
    }
}

void expect_throw(const std::function<void()>& f, const std::string& what) {
    bool threw = false;
    try {
        f();
    } catch (const std::invalid_argument&) {
        threw = true;
    }
    check(threw, "expected invalid_argument: " + what);
}

QueryResult run(const Layout& l, const QuerySpec& q, const ExecOptions& o) {
    if (q.op == Op::Sum)
        return q.where.empty() ? l.scan_sum(q.project) : l.filter_sum(q.where, q.project, o);
    return l.filter_materialize(q.where, q.project, o);
}

std::vector<std::string> projections_for(int cols) {
    std::vector<std::string> p = {"all", "0", std::to_string(cols - 1)};
    if (cols >= 4) p.insert(p.end(), {"0-3", "1-2", "3,0,2"});  // prefix, middle, unordered
    if (cols >= 16) p.insert(p.end(), {"0,4,8,12", "15,7"});     // spread
    return p;
}

std::vector<std::string> wheres_for(int cols) {
    std::vector<std::string> w = {"none", "c0<10", "c0<50", "c0=42", "c0=10..19",
                                  "c0>=0",   // everything
                                  "c0=500",  // nothing
                                  "c0<1"};
    if (cols >= 2) w.insert(w.end(), {"c1<50,c0<50", "c0<1,c1<50"});
    if (cols >= 13) w.push_back("c0<20,c12>=50,c5=0..49");
    return w;
}

void test_layouts() {
    struct Shape {
        std::size_t rows;
        std::size_t cols;
        std::vector<std::string> dists;  // --dist assignments
    };
    const std::vector<Shape> shapes = {
        {10007, 16, {}},
        {10007, 16, {"c0=sorted"}},
        {10007, 16, {"default=clustered:run=37"}},
        {1000, 3, {}},
        {2049, 64, {"c1=sorted"}},
        {1, 16, {}},
        {7, 5, {}},
    };
    const std::vector<std::size_t> batches = {1, 7, 1024, 1 << 20};
    const std::vector<FilterMode> filters = {FilterMode::Branchy, FilterMode::Branchless};

    for (const Shape& s : shapes) {
        DistSpec dists;
        for (const std::string& d : s.dists) dists.add(d);
        Table table = generate_table(s.rows, s.cols, 7, dists);

        std::vector<std::string> layout_texts = {"nsm", "dsm"};
        const std::size_t row_bytes = s.cols * 8;
        for (std::size_t page : {std::size_t{1024}, std::size_t{4096}, std::size_t{65536}})
            if (page >= row_bytes) layout_texts.push_back("pax:page=" + std::to_string(page));
        // The smallest page that holds one tuple, rounded up to 64 bytes.
        layout_texts.push_back("pax:page=" + std::to_string((row_bytes + 63) / 64 * 64));

        std::vector<std::unique_ptr<Layout>> layouts;
        for (const std::string& t : layout_texts)
            layouts.push_back(make_layout(parse_layout_spec(t), table));

        // Point lookups: every projection, id distribution and count (including more lookups
        // than rows, so ids repeat, and the first/last rows).
        for (const std::string& p : projections_for(static_cast<int>(s.cols))) {
            for (const char* dist : {"uniform", "sorted"}) {
                for (std::size_t n : {std::size_t{1}, std::size_t{3}, std::size_t{5000}}) {
                    QuerySpec q = parse_query_spec("op=lookup;cols=" + p + ";n=" +
                                                       std::to_string(n) + ";dist=" + dist,
                                                   static_cast<int>(s.cols));
                    RowIds ids = lookup_ids(q, s.rows);
                    check(ids.size() == n, "lookup id count");
                    if (n == 5000) ids[0] = 0, ids[1] = s.rows - 1;
                    QueryResult want = reference_lookup(table, ids, q.project);
                    for (const auto& l : layouts)
                        check(l->lookup(ids, q.project) == want,
                              l->name() + " " + std::to_string(s.rows) + "x" +
                                  std::to_string(s.cols) + " " + q.label());
                }
            }
        }

        for (const std::string& w : wheres_for(static_cast<int>(s.cols))) {
            for (const std::string& p : projections_for(static_cast<int>(s.cols))) {
                for (Op op : {Op::Sum, Op::Materialize}) {
                    QuerySpec q = parse_query_spec(
                        std::string("op=") + op_name(op) + ";cols=" + p + ";where=" + w,
                        static_cast<int>(s.cols));
                    QueryResult want = reference_result(table, q);
                    for (const auto& l : layouts)
                        for (std::size_t b : batches)
                            for (FilterMode f : filters) {
                                ExecOptions o;
                                o.dsm_batch = b;
                                o.filter = f;
                                QueryResult got = run(*l, q, o);
                                check(got == want,
                                      l->name() + " " + std::to_string(s.rows) + "x" +
                                          std::to_string(s.cols) + " " + q.label() + " batch " +
                                          std::to_string(b) + " " + filter_mode_name(f));
                            }
                }
            }
        }
    }
}

void test_parsers() {
    // Projections.
    check(parse_projection("all", 4).cols == std::vector<int>({0, 1, 2, 3}), "cols=all");
    check(parse_projection("0-2,5", 8).cols == std::vector<int>({0, 1, 2, 5}), "cols=0-2,5");
    check(parse_projection("c3-c4", 8).cols == std::vector<int>({3, 4}), "cols=c3-c4");
    check(parse_projection("0-3", 8).label() == "c0..c3", "label c0..c3");
    check(parse_projection("0,4", 8).label() == "c0|c4", "label c0|c4");
    check(parse_projection("2-3", 8).contiguous_first() == 2, "contiguous_first");
    check(parse_projection("3,2", 8).contiguous_first() == -1, "not contiguous");
    expect_throw([] { parse_projection("8", 8); }, "column out of range");
    expect_throw([] { parse_projection("3-1", 8); }, "empty range");
    expect_throw([] { parse_projection("0,,1", 8); }, "empty item");

    // Query specs.
    QuerySpec q = parse_query_spec("op=materialize;cols=0,4;where=c0<5,c3>=2", 8);
    check(q.op == Op::Materialize && q.project.size() == 2 && q.where.size() == 2, "query spec");
    check(q.where_label == "c0<5 & c3>=2", "where label");
    check(q.label() == "materialize|c0|c4|c0<5 & c3>=2", "query label");
    check(parse_query_spec("op=sum", 8).project.size() == 8, "default cols = all");
    check(parse_query_spec("op=sum;where=none", 8).where.empty(), "where=none");
    expect_throw([] { parse_query_spec("op=scan", 8); }, "unknown op");
    expect_throw([] { parse_query_spec("op=sum;colz=1", 8); }, "unknown key");
    expect_throw([] { parse_query_spec("op=sum;where=c9<1", 8); }, "predicate column range");
    expect_throw([] { parse_query_spec("op=sum;label=a,b", 8); }, "label with comma");
    expect_throw([] { parse_query_spec("op=lookup;where=c0<1", 8); }, "lookup with where");
    expect_throw([] { parse_query_spec("op=lookup;cols=0", 8); }, "lookup without n");
    expect_throw([] { make_row_ids("zipfish", 10, 5, 1); }, "unknown row-id dist");
    expect_throw([] { make_row_ids("uniform:x=1", 10, 5, 1); }, "row-id dist params");
    {
        QuerySpec l = parse_query_spec("op=lookup;cols=0-1;n=100;dist=sorted;seed=3", 8);
        check(l.label() == "lookup|c0..c1|n=100|sorted", "lookup label");
        RowIds a = lookup_ids(l, 1000), b = lookup_ids(l, 1000);
        check(a == b, "lookup ids deterministic");
        bool sorted = true, in_range = true;
        for (std::size_t i = 0; i < a.size(); ++i) {
            in_range = in_range && a[i] < 1000;
            if (i) sorted = sorted && a[i - 1] <= a[i];
        }
        check(sorted && in_range, "sorted ids ascending and in range");
    }

    // Layout specs.
    LayoutSpec ls = parse_layout_spec("pax:page=4K");
    check(ls.name == "pax" && ls.params.at("page") == "4K", "layout spec");
    Table t = generate_table(100, 4, 1);
    expect_throw([&] { make_layout(parse_layout_spec("columnar"), t); }, "unknown layout");
    expect_throw([&] { make_layout(parse_layout_spec("pax:pages=4K"), t); }, "unknown param");
    expect_throw([&] { make_layout(parse_layout_spec("nsm:page=4K"), t); }, "nsm has no params");
    expect_throw([&] { make_layout(parse_layout_spec("pax:page=100"), t); }, "page not x64");
    expect_throw([&] { make_layout(parse_layout_spec("pax:page=x"), t); }, "bad page size");

    // Distributions.
    DistSpec d;
    expect_throw([&] { d.add("c0=gaussian"); }, "unknown distribution");
    expect_throw([&] { d.add("c0=uniform:mean=3"); }, "unknown distribution key");
    expect_throw([&] { d.add("x=uniform"); }, "bad --dist target");
    expect_throw([&] { d.add("c0=uniform:lo=5:hi=1"); }, "empty range");
    d.add("c0=sorted:lo=0:hi=9");
    d.add("c1=clustered:run=10");
    Table s = generate_table(100, 3, 1, d);
    bool sorted = true, in_range = true, runs = true;
    for (std::size_t r = 1; r < s.rows; ++r) sorted = sorted && s.at(r - 1, 0) <= s.at(r, 0);
    for (std::size_t r = 0; r < s.rows; ++r) in_range = in_range && s.at(r, 0) >= 0 && s.at(r, 0) <= 9;
    for (std::size_t r = 0; r < s.rows; ++r) runs = runs && s.at(r, 1) == s.at(r / 10 * 10, 1);
    check(sorted && s.at(0, 0) == 0 && s.at(99, 0) == 9, "sorted covers [lo, hi] ascending");
    check(in_range, "sorted stays in range");
    check(runs, "clustered runs share a value");
    Table a = generate_table(50, 3, 9), b = generate_table(50, 3, 9);
    check(a.values == b.values, "generation is deterministic");
}

}  // namespace

int main() {
    try {
        test_parsers();
        test_layouts();
    } catch (const std::exception& e) {
        std::cerr << "FAIL: unexpected exception: " << e.what() << "\n";
        return 1;
    }
    std::cout << (g_failures ? "FAILED " : "OK ") << g_checks - g_failures << "/" << g_checks
              << " checks passed\n";
    return g_failures ? 1 : 0;
}
