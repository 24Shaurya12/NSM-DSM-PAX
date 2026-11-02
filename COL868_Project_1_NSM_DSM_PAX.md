# COL868 / COL8395: Course Projects

## General submission requirements

The course project must be completed by the end of October. For each project, students must submit working code (zip file) and a report of 6–8 pages in the two-column ACM format (LaTeX template provided as Overleaf project). The report should describe a clear research question, the implemented system, a reproducible experimental evaluation, and an explanation of the observed results (see suggested outline for each project below).

Do not change the template, margins, font size, etc.

# Project 1: Comparing NSM, DSM, and PAX

## Project goal

The goal of this project is to investigate how physical data layout affects analytical query performance. Your goal is to determine when a row-oriented layout (NSM), a column-oriented layout (DSM), or a cache-conscious page layout (PAX) performs best as query projectivity and selectivity change.

The project should identify the crossover points between the three layouts and explain them in terms of useful bytes accessed, memory bandwidth, cache behavior, and tuple-reconstruction cost.

## What to implement

Implement a small in-memory scan engine in C++ or Rust. The same fixed-width relational schema must be represented using:

1. **NSM:** all attributes of a tuple are stored together.
2. **DSM:** each attribute is stored in a separate contiguous array.
3. **PAX:** tuples are divided into fixed-size logical pages, with each page internally divided into attribute minipages.

The complete dataset must be loaded into memory before query execution begins. Data loading and layout construction must not be included in query execution time. Although PAX pages reside in memory, their logical page boundaries must be preserved.

The engine must support:

- scanning a table;
- equality and range predicates on one or more attributes;
- projection of a configurable subset of attributes;
- a simple aggregation such as SUM, COUNT, or AVG; and
- reconstruction of complete qualifying tuples.

Students must construct a benchmark that varies:

- the fraction of columns accessed;
- predicate selectivity;
- table width;
- working-set size, including sizes larger than the last-level cache; and
- PAX page or minipage size.

The evaluation must report query latency and at least one cache- or memory-related hardware counter, such as LLC misses or memory bandwidth. The implementation need not include disk I/O, a buffer pool, compression, concurrency control, or a SQL parser.

## Report structure

1. **Introduction and research question:** Why physical layout matters and which crossover points are being studied.
2. **Design and implementation:** Representation of NSM, DSM, and PAX; supported operators; and important implementation decisions.
3. **Experimental methodology:** Data, workloads, hardware, metrics, and measurement protocol.
4. **Results:** Performance as projectivity, selectivity, working-set size, and PAX page size change.
5. **Analysis:** Explanation of the observed crossover points using bytes accessed, cache behavior, and reconstruction cost.
6. **Conclusions.**
