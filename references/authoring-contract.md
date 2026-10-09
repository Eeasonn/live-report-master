# Authoring contract (v1)

Agent authors `report_request.json`; it does not author arbitrary HTML. `spec_to_report.py` owns the HTML, CSS, GridStack coordinates and ECharts options. The only visual template is `standard-bi-v1`.

## Request boundary

Use `report-request.schema.json` and `validate_request.py`. `columnRoles` is optional — columns you mark are respected, unmarked ones are auto-inferred from the profile; only unknown column references are rejected. Charts use only these presets: `bar.rank`, `bar.stacked`, `line.trend`, `area.trend`, `scatter.correlation`, `pie.share`, `donut.share`, `funnel.stage`, `table.aggregate`, `table.detail`.

Deleted in v1: pictorial bar, boxplot, treemap, sunburst, tree, theme river, radar, gauge, map, graph, sankey, parallel, candlestick, effectScatter, custom/lines and editable background color. A request containing one of them fails validation; do not emulate it with another preset.

## Generated document

The compiler emits one complete HTML document with an `ai-report-manifest` JSON script, a `grid-stack` report grid, stable block IDs and chart target IDs. The fixed order is cover, filter, KPI cards, charts, insights. Text marked `data-editable-text` is atomic and editable only after clicking the single 编辑 button.

## Chart data contract

Each generated editable chart has `editor.editable: true` and `editor.data` containing `datasetId`, primary `dimension`, selected `measures`, aggregation, optional `roles.breakdown`, optional time `dimensionBucket`, measureTypes and filter. Aggregations are `sum`, `average`, `count`, `min`, `max`. Tables use the same contract and can switch to a chart view.

The compiler always emits JSON-safe ECharts options. No function-valued or function-string formatter is allowed. Semantic measure types (`currency`, `percent`, `count`, `duration`, `number`, `unitless`) drive deterministic display formatting; source values remain unchanged.

## Runtime behavior

All standard controls apply on `change`/`input` without an Apply button. In singlefile mode aggregation runs over embedded rows. In server mode the browser sends bounded requests to `/api/v1`; raw rows never enter the manifest, and a per-chart render sequence discards stale responses so rapid dimension/measure changes cannot blank a chart.

## Validation and packaging

`validate_report.py` checks IDs, schema, viewType whitelist, external-resource policy and server row stripping. `lint_report.py` is advisory unless `--strict`. `package_report.py` inlines ECharts, GridStack and runtime assets for singlefile output. `audit_editor.mjs` checks painted content, live controls, data truth, responsive overflow and BI insertion.
