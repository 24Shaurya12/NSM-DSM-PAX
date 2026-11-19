CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -march=native -Wall -Wextra -Wpedantic

SRCS := $(wildcard src/*.cpp)
OBJS := $(SRCS:src/%.cpp=build/%.o)
LIB_OBJS := $(filter-out build/main.o,$(OBJS))
BIN  := build/scan_bench

CAL_BIN := build/calibrate

TEST_SRCS := $(wildcard tests/*.cpp)
TEST_OBJS := $(TEST_SRCS:tests/%.cpp=build/tests/%.o)
TEST_BIN  := build/engine_tests

DEPS := $(OBJS:.o=.d) $(TEST_OBJS:.o=.d) build/tools/calibrate.d

all: $(BIN) $(CAL_BIN)

$(BIN): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

$(TEST_BIN): $(TEST_OBJS) $(LIB_OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

$(CAL_BIN): build/tools/calibrate.o $(LIB_OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

test: $(TEST_BIN)
	./$(TEST_BIN)

build/%.o: src/%.cpp | build
	$(CXX) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/tests/%.o: tests/%.cpp | build/tests
	$(CXX) $(CXXFLAGS) -Isrc -MMD -MP -c $< -o $@

build/tools/%.o: tools/%.cpp | build/tools
	$(CXX) $(CXXFLAGS) -Isrc -MMD -MP -c $< -o $@

build build/tests build/tools:
	mkdir -p $@

clean:
	rm -rf build

-include $(DEPS)

.PHONY: all test clean
