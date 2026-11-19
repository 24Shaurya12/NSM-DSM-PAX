"""The model ladder (plan §5.5): v0 bytes/bandwidth, v1 Manegold et al. as published, v2 + modern
memory system, v3 + modern CPU. Same patterns and CPU terms; each rung switches on more
mechanisms, so the validation can show which mechanisms matter.

Fittable parameters (plan §5.6; each has a physical meaning, defaults = no correction):
  k_mem      scale on memory time          k_cpu     scale on calibrated CPU work
  k_branch   scale on c_branch_miss        k_over    scale on per-batch / per-page overheads
  gamma      overlap: T = max(Tm, Tc) + gamma * min(Tm, Tc)   (v3 only)
  pax_lookup_extra  cycles per PAX lookup for locating the page and slot (a division)
"""
from .base import CostBreakdown, CostModel
from .patterns.describe import describe
from .patterns.evaluate import memory
from .patterns.regions import line_stats
from .registry import register_model


def resolve_cycles(hw, name, arg, params):
    """Calibrated cycles for one CPU term (see describe.py for where each is used)."""
    c = hw.cycles
    if name == "nsm_copy":
        base = c("c_nsm_pred")
        a1, a16 = c("c_copy_tuple_k1") - base, c("c_copy_tuple_k16") - base
        return max(0.0, a1 + (a16 - a1) * (arg - 1) / 15.0)
    if name == "pax_gather_value":
        return max(0.0, (c("pax_mat_k16_per_row") - c("pax_pred_per_row")) / 16.0)
    if name == "pax_lookup_extra":
        return params.get("pax_lookup_extra", 0.0)
    if name == "dsm_sum_value":
        return c("c_sum_value", c("c_gather_value"))
    table = {
        "nsm_row": "c_nsm_row", "nsm_value": "c_nsm_value", "nsm_pred": "c_nsm_pred",
        "dsm_scan": "c_scan", "dsm_pred": "c_pred", "dsm_pred_branchless": "c_pred_branchless",
        "dsm_refine": "c_select_sum", "dsm_refine_branchless": "c_select_sum_branchless",
        "dsm_select_sum": "c_select_sum", "dsm_gather_k1": "c_gather_k1",
        "dsm_gather_value": "c_gather_value", "pax_scan": "pax_scan_per_row",
        "pax_pred": "pax_pred_per_row", "pax_pred_branchless": "c_pred_branchless",
        "o_batch": "o_batch", "pax_scan_o_page": "pax_scan_o_page",
        "pax_pred_o_page": "pax_pred_o_page",
    }
    return max(0.0, c(table[name]))


class PatternModel(CostModel):
    b_eff = overfetch = mlp = False
    branches = overheads = overlap = False
    params = {"k_mem": 1.0, "k_cpu": 1.0, "k_branch": 1.0, "k_over": 1.0, "gamma": 1.0,
              "pax_lookup_extra": 0.0}

    def components(self, storage, exec_opts, query, data):
        """Unscaled parts: (mem cost, base cpu ns, branch ns, overhead ns, description)."""
        d = describe(storage, exec_opts, query, data)
        hw = self.hw
        mem = memory(d.pattern, hw, self.b_eff, self.overfetch, self.mlp, self.mlp)
        terms = {}
        for name, count, arg in d.cpu:
            terms[name] = terms.get(name, 0.0) + count * resolve_cycles(hw, name, arg,
                                                                         self.params) / hw.freq
        over = sum(n * resolve_cycles(hw, name, arg, self.params) for name, n, arg in d.overhead
                   ) / hw.freq if self.overheads else 0.0
        branch = d.branch_misses * hw.cycles("c_branch_miss") / hw.freq if self.branches else 0.0
        return mem, terms, branch, over, d

    def combine(self, mem_ns, cpu_ns):
        p = self.params
        if self.overlap:
            return max(mem_ns, cpu_ns) + p["gamma"] * min(mem_ns, cpu_ns)
        return mem_ns + cpu_ns

    def predict(self, storage, exec_opts, query, data):
        mem, terms, branch, over, d = self.components(storage, exec_opts, query, data)
        p = self.params
        mem_ns = p["k_mem"] * mem.total_ns
        cpu_ns = p["k_cpu"] * sum(terms.values()) + p["k_branch"] * branch + p["k_over"] * over
        terms = dict(terms, branch=branch, overhead=over)
        return CostBreakdown(self.combine(mem_ns, cpu_ns), mem_ns, cpu_ns,
                             p["k_mem"] * mem.seq_ns, p["k_mem"] * mem.rand_ns,
                             p["k_mem"] * mem.tlb_ns, mem.dram_bytes, mem.misses,
                             d.branch_misses, terms)


@register_model("v0")
class BytesOverBandwidth(PatternModel):
    """time = bytes of the cache lines touched / sequential DRAM bandwidth."""

    def predict(self, storage, exec_opts, query, data):
        mem, *_ = self.components(storage, exec_opts, query, data)
        s, r = mem.misses[self.hw.last_cache.name]
        bytes_ = (s + r) * self.hw.last_cache.B
        t = self.params["k_mem"] * bytes_ / self.hw.dram_gb_s
        return CostBreakdown(t, t, 0.0, t, 0.0, 0.0, bytes_, mem.misses)


@register_model("v1")
class Manegold(PatternModel):
    """Manegold, Boncz & Kersten (2002) as published: per-level misses, T_mem + T_cpu."""


@register_model("v2")
class ModernMemory(PatternModel):
    """v1 + effective transfer unit, prefetch over-fetch, memory-level parallelism, hidden
    page walks in sequential streams."""
    b_eff = overfetch = mlp = True


@register_model("v3")
class Modern(PatternModel):
    """v2 + branch mispredictions, per-batch/per-page overheads, memory/CPU overlap."""
    b_eff = overfetch = mlp = True
    branches = overheads = overlap = True
    params = dict(PatternModel.params, gamma=0.5)


__all__ = ["BytesOverBandwidth", "Manegold", "ModernMemory", "Modern", "line_stats"]
