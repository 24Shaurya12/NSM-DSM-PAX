CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -march=native -Wall -Wextra -Wpedantic
CPPFLAGS += -Isrc

SRCS := $(wildcard src/*.cpp src/queries/*.cpp)
OBJS := $(SRCS:src/%.cpp=build/%.o)
DEPS := $(OBJS:.o=.d)
BIN  := build/scan_bench

# Quick test: one low projectivity (k = 1) and one high projectivity (k = N).
TEST_ROWS ?= 100000
TEST_COLS ?= 512

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

all: $(BIN)

$(BIN): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

build/%.o: src/%.cpp
	@mkdir -p $(@D)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

test: $(BIN)
	./$(BIN) --rows $(TEST_ROWS) --cols $(TEST_COLS) --k 1,$(TEST_COLS) $(QUERY_FLAGS) $(PERF_FLAGS) \
		| python3 scripts/summarize.py

# Graphs: every k (1, 2, 4, ..., N) for every layout. Query qN is written to
# output/qN/graph_qN.csv and output/qN/graph_qN.png (graph_qN_log.png with `log`).
# With `perf`, each counter metric also gets its own output/qN/graph_qN_<metric>.png.
VENV := .venv

define GRAPH_ONE
mkdir -p output/$(1)
./$(BIN) --rows $(TEST_ROWS) --cols $(TEST_COLS) --query $(QUERY_$(1)) $(PERF_FLAGS) --output output/$(1)/graph_$(1).csv
$(VENV)/bin/python scripts/plot.py output/$(1)/graph_$(1).csv output/$(1)/graph_$(1) $(if $(LOGY),--logy)

endef

graph: $(BIN) $(VENV)/.installed
	$(foreach q,$(RUN_IDS),$(call GRAPH_ONE,$(q)))

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt
	touch $@

# Selectors and `log` are modifiers for test/graph; on their own (`make q1`) they run the test.
$(SELECTORS) $(LOG_WORDS) $(PERF_WORDS): $(if $(filter test graph,$(MAKECMDGOALS)),,test)
	@:

clean:
	rm -rf build

-include $(DEPS)

.PHONY: all test graph clean $(SELECTORS) $(LOG_WORDS) $(PERF_WORDS) q1 Q1 q2 Q2
