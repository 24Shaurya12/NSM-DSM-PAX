CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -march=native -Wall -Wextra -Wpedantic

SRCS := $(wildcard src/*.cpp)
OBJS := $(SRCS:src/%.cpp=build/%.o)
DEPS := $(OBJS:.o=.d)
BIN  := build/scan_bench

all: $(BIN)

$(BIN): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

build/%.o: src/%.cpp | build
	$(CXX) $(CXXFLAGS) -MMD -MP -c $< -o $@

build:
	mkdir -p build

clean:
	rm -rf build

-include $(DEPS)

.PHONY: all clean
