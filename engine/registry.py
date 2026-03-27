from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

from config import AppConfig
from engine.base import BaseBacktestEngine

logger = logging.getLogger(__name__)

_IGNORED_MODULES = {"__init__", "base", "jobs", "registry", "tradebook"}


def _iter_subclasses(base_class: type[BaseBacktestEngine]) -> list[type[BaseBacktestEngine]]:
    discovered: list[type[BaseBacktestEngine]] = []
    for subclass in base_class.__subclasses__():
        discovered.append(subclass)
        discovered.extend(_iter_subclasses(subclass))
    return discovered


def discover_backtest_engine_classes() -> list[type[BaseBacktestEngine]]:
    import engine as engine_package

    for module_info in pkgutil.iter_modules(engine_package.__path__):
        if module_info.name in _IGNORED_MODULES:
            continue
        module_name = f"{engine_package.__name__}.{module_info.name}"
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            logger.warning("Skipping engine module %s due to import error: %s", module_name, exc)

    classes: dict[str, type[BaseBacktestEngine]] = {}
    for candidate in _iter_subclasses(BaseBacktestEngine):
        if inspect.isabstract(candidate):
            continue
        engine_name = getattr(candidate, "engine_name", "") or candidate.__name__.lower()
        classes.setdefault(engine_name, candidate)

    return [classes[name] for name in sorted(classes)]


def build_backtest_engines(config: AppConfig) -> list[BaseBacktestEngine]:
    classes = discover_backtest_engine_classes()
    if not classes:
        raise RuntimeError("No backtest engines were discovered in the engine package.")
    return [engine_class(config) for engine_class in classes]
