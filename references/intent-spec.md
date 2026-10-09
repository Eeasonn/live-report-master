# Intent DSL (Flint-style) — v1

This is the authoring intent language for `live-report-master`. It mirrors
Flint's core idea: the author/agent declares *what each field is* and *what
chart they want*; a deterministic compiler derives every low-level parameter
(chart type, axis, aggregation, formatting, series type, Y-axis). The author
never writes ECharts options, series types, axis indices, or bar spacing.

It is **optional and additive**: a classic `presetId`+`encoding` chart and an
`chartSpec` chart can coexist in one request. `compile_intent.py` turns a
`chartSpec` into the same internal chart config the compiler already consumes.

## Inputs (top-level, optional)

### `semantic_types`
Field → a semantic type (T0/T1/T2) or an annotation object. Drives encoding,
formatting, aggregation role, zero-baseline and non-additivity.

```jsonc
"semantic_types": {
  "订单日期":   {"type": "DateTime", "timeUnit": "month"},
  "商品销售额": {"type": "Revenue", "unit": "元"},
  "实付金额":   {"type": "Revenue"},
  "客户地区":   {"type": "Category"},
  "毛利率":     {"type": "Ratio"}
}
```

Accepted type labels (all degrade to a canonical T2 format without failing):
- T2 formats: `currency`, `percent`, `count`, `duration`, `number`, `unitless`
- T1 families: `money`/`revenue`/`Revenue`, `ratio`/`Rate`, `count`, `duration`, `number`
- T0 families: `measure` (→ `number`), `dimension`/`category`/`time`
- Chinese tokens: `金额`/`销售额`/`收入` → currency, `占比`/`率` → percent, `人数` → count
  (see `scripts/semantics/type_registry.py`)

### `chart.chartSpec`
One chart's intent. `chartType` + `encodings` (channel bindings). The x channel
is the category axis; y is one or more measures (1–10 on a single cartesian
dimension); an optional `breakdown` adds a 2nd dimension (cross-tab); per-measure
`series` (bar/line) and `axis` (left/right) enable composite (mixed) charts with
≤2 Y axes.

```jsonc
{
  "id": "sales_margin",
  "chartSpec": {
    "chartType": "mixed",
    "title": "月度销售与毛利",
    "encodings": {
      "x": {"field": "订单日期", "timeUnit": "month"},
      "y": [
        {"measure": "商品销售额", "series": "bar",  "axis": "left"},
        {"measure": "实付金额",   "series": "line", "axis": "right"}
      ]
    }
  }
}
```

### `chartType`
Maps to a skill viewType (ECharts family): `mixed`→bar, `bar`→bar,
`stackedBar`→stackedBar, `line`→line, `area`→area, `pie`→pie, `donut`→donut,
`funnel`→funnel, `scatter`→scatter, `table`→table.

## Compile chain

```
semantic_types + chartSpec
  → compile_chart_spec()            (scripts/compile_intent.py)
      resolve semantic types → FieldSemantics  (semantics/type_registry)
      channel rules (degrades category→count, rejects non-additive stack)
      derive dimension / measures / aggregation / seriesType / seriesAxis /
      dimensionBucket / roles(breakdown)
  → spec_to_report.normalize_request   (expand chartSpec → preset chart)
  → _chart_manifest_entry              (editor.data carries seriesType/seriesAxis
                                        /dimensionBucket for the runtime)
  → runtime optionFor()                (categoryOption renders mixed series + ≤2 Y axes)
```

Rules that the compiler enforces on intent:
- A non-additive measure (percent/unitless) defaults to `average`, never `sum`.
- A category dragged into a measure slot degrades to a row-count.
- Composite (`series` bar/line) only on a cartesian single-dimension chart;
  2-维度 cross-tab caps measures at 1; single cartesian dimension allows up to 10.
- `breakdown` (2nd dimension) only on `bar`/`stackedBar`/`table`.

## Flint is moved into the skill (single codebase)

Flint's defining semantics are **transcribed into the skill** as first-class,
skill-owned modules — no external `flint-chart` runtime, no adapter bridge. The
`refs/flint/` directory is the authoritative SOURCE for tracing each rule, not
a runtime dependency. ECharts-only backend; the canvas / editable-HTML / KPI /
packaging layers stay the skill's own.

## Standard rules module (`scripts/semantics/`)

| module | role | mirrors Flint |
|---|---|---|
| `flint_core.py` | ported type-registry (t0/t1/visEncodings/aggRole/domainShape/zeroBaseline/zeroPad) + `inferVisCategory` + field semantics | `type-registry.ts` / `semantic-types.ts` |
| `recommend.py` | `build_profile` + `recommend_chart_types` (scored chart-type suggestions) | `chart-type-recommendation.ts` |
| `type_registry.py` | T0/T1/T2 resolution + per-type rules | `semantic-types.ts`/`type-registry.ts` |
| `channel_semantics.py` | channel action + Flint-grade `resolve_channel_semantics` (encoding/aggregationDefault/additive/stackable/sort) | `resolve-semantics.ts` / `channelAction` |
| `shape_rules.py` | per-chartType capacity + composite budget | `shapeRules`/`effMeasureMax` |

## skill ↔ Flint source map

| skill | Flint source (vendored `refs/flint`) |
|---|---|
| `scripts/measure_semantics.py` | `packages/flint-js/src/core/semantic-types.ts`, `type-registry.ts` |
| `scripts/semantics/type_registry.py` | `semantic-types.ts`, `type-registry.ts` |
| `scripts/semantics/channel_semantics.py` | `resolve-semantics.ts` (ChannelSemantics) |
| `scripts/semantics/shape_rules.py` | `shapeRules` in `chart-adapter.js`; Flint `computeChannelBudgets` |
| `scripts/spec_to_report.py` (`_chart_option`, `_flint_value_axis`, `_series_from_rows`) | `instantiate-spec.ts` + `templates/{bar,line,area}.ts` + Flint `makeZeroDecision`/`computePaddedDomain` |
| `assets/runtime/chart-adapter.js` (`categoryOption`/`composite`/`patSemantics`/`flintZeroDecision`/`recommendChartTypes`) | ECharts per-series `{type, yAxisIndex}` + moved-in Flint zero-baseline/JS recommend |
| `assets/runtime/chart-adapter.js` (`flintInferVisCategory`/`flintFieldSemantics`/`flintEncoding`) | JS port of `flint_core` (axis kind for live editing) |

## Validation

- `chartSpec` is validated by `validate_request` (must be an object; presetId may
  be omitted; compile raises `IntentError` on an invalid shape/over-capacity).
- The ECharts contract is grounded in `refs/echarts/echarts.d.ts` (per-series
  `type` line 8381, `type?: 'bar'` 11174, `yAxisIndex` 3351/8381).
