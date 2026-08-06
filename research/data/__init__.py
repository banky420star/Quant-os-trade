"""Research data layer — history export, catalog, symbol mapping, quality checks."""

from .catalog import (
    RESEARCH_UNIVERSE,
    TIMEFRAMES,
    available_symbols,
    available_timeframes,
    build_catalog,
    parquet_path,
    read_parquet,
    train_test_split_date,
)
from .export_mt5_history import export_all, export_manifest, export_to_csv, export_to_parquet_flat
from .quality_checks import QualityReport, quality_summary, run_quality_checks
from .symbol_map import (
    broker_to_logical,
    logical_to_broker,
    record_rename,
    register_symbol_pair,
    rename_history,
)

__all__ = [
    "RESEARCH_UNIVERSE",
    "TIMEFRAMES",
    "QualityReport",
    "available_symbols",
    "available_timeframes",
    "broker_to_logical",
    "build_catalog",
    "export_all",
    "export_manifest",
    "export_to_csv",
    "export_to_parquet_flat",
    "logical_to_broker",
    "parquet_path",
    "quality_summary",
    "read_parquet",
    "record_rename",
    "register_symbol_pair",
    "rename_history",
    "run_quality_checks",
    "train_test_split_date",
]
