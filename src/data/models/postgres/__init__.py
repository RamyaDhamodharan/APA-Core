import importlib
import pkgutil
import src.models

# Expose all existing PostgreSQL models through src.data.models.postgres
for _, module_name, _ in pkgutil.iter_modules(src.models.__path__):
    module = importlib.import_module(f"src.models.{module_name}")
    globals()[module_name] = module
