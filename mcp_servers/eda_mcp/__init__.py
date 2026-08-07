"""Exploratory Data Analysis (EDA) MCP server suite.

Five focused MCP servers that together let any MCP host (Fox chat, Claude,
Cursor, LangChain, ...) profile a dataset, analyse it, visualise it and
compile a professional EDA report:

    1. eda-data-profiler      load / schema / basic profile / quality issues
    2. eda-univariate         single-variable stats + missing-data analysis
    3. eda-multivariate       correlations, target relationships, PCA, clustering
    4. eda-visualizer         plot generation + auto-visualization + insights
    5. eda-report-generator   Markdown / HTML / PDF report compilation

All servers share a disk-backed :class:`DatasetStore` keyed by ``dataset_id``,
so intermediate artifacts (parquet, metadata, plots) pass between processes by
reference. See ``README.md`` for setup, client configuration and an example
end-to-end workflow.
"""

__version__ = "0.1.0"

EDA_WORKSPACE_ENV = "FOX_EDA_WORKSPACE"
