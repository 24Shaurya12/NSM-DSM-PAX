"""Model registry: @register_model("v1") class MyModel(CostModel): ..."""
from ..registry import Registry

MODELS = Registry("cost model")
register_model = MODELS.register


def make(name, hw, **params):
    return MODELS.get(name)(hw, **params)
