CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O3 -march=native -Wall -Wextra -Wpedantic
CPPFLAGS += -Isrc

# Output directory; `make BUILD=build-dev` builds a second copy (e.g. while experiments run
# from build/).
BUILD ?= build

SRCS := $(wildcard src/*.cpp src/queries/*.cpp src/encodings/*.cpp)
OBJS := $(SRCS:src/%.cpp=$(BUILD)/%.o)
DEPS := $(OBJS:.o=.d)
BIN  := $(BUILD)/scan_bench

# Quick test: one low projectivity (k = 1) and one high projectivity (k = N).
TEST_ROWS ?= 1000000
TEST_COLS ?= 16

# Query ids: `make test q1` / `make graph q2` (case-insensitive) restrict test/graph to
# those queries; without one, all queries run. To add a query, add its id here and map it.
QUERY_IDS  := q1 q2
QUERY_q1   := sum
QUERY_q2   := projection
SELECTORS  := $(QUERY_IDS) $(shell echo $(QUERY_IDS) | tr a-z A-Z)

PICKED_IDS := $(sort $(filter $(QUERY_IDS),$(shell echo $(MAKECMDGOALS) | tr A-Z a-z)))
RUN_IDS    := $(or $(PICKED_IDS),$(QUERY_IDS))
QUERY_FLAGS := $(foreach q,$(RUN_IDS),--query $(QUERY_$(q)))

# `make graph q2 log` (case-insensitive) plots the y axis on a log scale.
LOG_WORDS  := log LOG Log
LOGY       := $(if $(filter $(LOG_WORDS),$(MAKECMDGOALS)),1)

# `make test q2 perf` / `make graph q2 perf` (case-insensitive) also record hardware
# counters with the `core` event set; `perf=mem` or `perf=stall` picks another set.
PERF_WORDS := perf PERF Perf
PERF_SET   := $(or $(perf),$(if $(filter $(PERF_WORDS),$(MAKECMDGOALS)),core))
PERF_FLAGS := $(if $(PERF_SET),--perf $(PERF_SET))

# Encodings (DSM only) and data:
#   enc=all | enc=dict,rle   also run DSM with every column encoded (plain bitpack for dict dictb rle delta)
#   mode=value | mode=vector scan mode for encoded layouts (default both)
#   dist=sorted              data distribution: uniform (default) wide sorted runs zipf
#   layout=dsm,nsm,pax|none  uncompressed layouts to run; defaults to all, or to dsm when enc= is set
# Query shape and execution:
#   sel=0.01,0.1             add a predicate on c0 with these selectivities (one run each)
#   strategy=early,late      execution strategies (default all but auto)
#   page=4096,65536          PAX page size(s) (TEST_PAGE_SIZE, default 16384)
#   batch=auto | batch=1024  rows per vector
#   knobs="--hugepages --dsm-offset auto --simd --nt-stores"   any other scan_bench flags
TEST_PAGE_SIZE ?= 16384
LAYOUT_SEL := $(or $(layout),$(if $(enc),dsm))
DATA_FLAGS := $(if $(enc),--encoding $(enc)) $(if $(mode),--mode $(mode)) \
              $(if $(dist),--dist $(dist)) $(if $(LAYOUT_SEL),--layout $(LAYOUT_SEL)) \
              $(if $(sel),--sel $(sel)) $(if $(strategy),--strategy $(strategy)) \
              --page-size $(or $(page),$(TEST_PAGE_SIZE)) $(if $(batch),--batch $(batch)) $(knobs)
# Graph file suffix, so encoded / non-uniform / filtered runs don't overwrite the plain graphs.
GRAPH_SUFFIX := $(if $(enc),_enc)$(if $(dist),_$(dist))$(if $(sel),_sel)

MEMBENCH := $(BUILD)/membench
BATCHBENCH := $(BUILD)/batchbench

all: $(BIN) $(MEMBENCH) $(BATCHBENCH)

$(BIN): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

# Standalone memory-hierarchy microbenchmark (latency / bandwidth vs working-set size).
$(MEMBENCH): src/tools/membench.cpp
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) -o $@ $<

# Vector size vs work per value and fixed cost per vector (which cache level to size for).
$(BATCHBENCH): src/tools/batchbench.cpp
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) -o $@ $<

$(BUILD)/%.o: src/%.cpp
	@mkdir -p $(@D)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

test: $(BIN)
	./$(BIN) --rows $(TEST_ROWS) --cols $(TEST_COLS) --k 1,$(TEST_COLS) $(QUERY_FLAGS) $(PERF_FLAGS) $(DATA_FLAGS) \
		| python3 scripts/summarize.py

# Graphs: every k (1, 2, 4, ..., N) for every layout. Query qN is written to
# output/qN/graph_qN.csv and output/qN/graph_qN.png (graph_qN_log.png with `log`).
# With enc= / dist=, the name gets _enc / _<dist>, e.g. output/q1/graph_q1_enc_sorted.png.
# With `perf`, each counter metric also gets its own output/qN/graph_qN_<metric>.png.
VENV := .venv

define GRAPH_ONE
mkdir -p output/$(1)
./$(BIN) --rows $(TEST_ROWS) --cols $(TEST_COLS) --query $(QUERY_$(1)) $(PERF_FLAGS) $(DATA_FLAGS) --output output/$(1)/graph_$(1)$(GRAPH_SUFFIX).csv
$(VENV)/bin/python scripts/plot.py output/$(1)/graph_$(1)$(GRAPH_SUFFIX).csv output/$(1)/graph_$(1)$(GRAPH_SUFFIX) $(if $(LOGY),--logy)

endef

graph: $(BIN) $(VENV)/.installed
	$(foreach q,$(RUN_IDS),$(call GRAPH_ONE,$(q)))

# `make encodings q1`: for every data distribution, run uncompressed DSM plus every
# encoding over every k, plot latency vs k (output/qN/graph_qN_enc_<dist>.png), then
# summarise compression ratio and speedup at k = N per distribution and encoding in
# output/qN/encodings_qN.{png,csv}. Honours mode=, perf, log, TEST_ROWS and TEST_COLS.
DISTS := uniform wide sorted runs zipf

define ENC_ONE
mkdir -p output/$(1)
./$(BIN) --rows $(TEST_ROWS) --cols $(TEST_COLS) --query $(QUERY_$(1)) $(PERF_FLAGS) --encoding all --layout dsm $(if $(mode),--mode $(mode)) --dist $(2) --output output/$(1)/graph_$(1)_enc_$(2).csv
$(VENV)/bin/python scripts/plot.py output/$(1)/graph_$(1)_enc_$(2).csv output/$(1)/graph_$(1)_enc_$(2) $(if $(LOGY),--logy)

endef

define ENC_QUERY
$(foreach d,$(DISTS),$(call ENC_ONE,$(1),$(d)))
$(VENV)/bin/python scripts/compare_encodings.py output/$(1)/encodings_$(1) $(foreach d,$(DISTS),output/$(1)/graph_$(1)_enc_$(d).csv)

endef

encodings: $(BIN) $(VENV)/.installed
	$(foreach q,$(RUN_IDS),$(call ENC_QUERY,$(q)))

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt
	touch $@

# Selectors and `log` are modifiers for test/graph; on their own (`make q1`) they run the test.
$(SELECTORS) $(LOG_WORDS) $(PERF_WORDS): $(if $(filter test graph encodings,$(MAKECMDGOALS)),,test)
	@:

# The full experiment suite behind the report (hours; see scripts/experiments.py --list),
# then the figures and the PDF. `make experiments only=selectivity,pax` runs a subset.
experiments: $(BIN) $(MEMBENCH)
	python3 scripts/experiments.py $(if $(only),--only $(only))

figures: $(VENV)/.installed
	$(VENV)/bin/python scripts/figures.py --png

report: figures
	cd report && latexmk -pdf -interaction=nonstopmode -halt-on-error report.tex

clean:
	rm -rf $(BUILD)

-include $(DEPS)

.PHONY: all test graph encodings experiments figures report clean $(SELECTORS) $(LOG_WORDS) $(PERF_WORDS) q1 Q1 q2 Q2
