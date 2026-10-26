import importlib
import pkgutil
import src.services

# Expose all existing services through src.core.services
for _, module_name, _ in pkgutil.iter_modules(src.services.__path__):
    module = importlib.import_module(f"src.services.{module_name}")
    globals()[module_name] = module
