"""A tiny name -> class registry, shared by every pluggable component (distribution models,
template families, and later cost models, search strategies and selectivity estimators)."""


class Registry:
    def __init__(self, kind):
        self.kind = kind
        self._items = {}

    def register(self, name):
        def deco(cls):
            if name in self._items:
                raise ValueError(f"{self.kind} '{name}' registered twice")
            self._items[name] = cls
            cls.registered_name = name
            return cls
        return deco

    def get(self, name):
        try:
            return self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items))
            raise ValueError(f"unknown {self.kind} '{name}' (known: {known})") from None

    def names(self):
        return sorted(self._items)
