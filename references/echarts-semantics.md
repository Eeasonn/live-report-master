# Official ECharts semantics (source: apache-echarts-6.1.0-src.zip)

This reference pins the **authoritative** ECharts option/component fields that the
v1 compiler is allowed to emit. It is extracted from the official Apache ECharts
6.1.0 TypeScript source (`src/**`), NOT invented by the model. The Skill's
deterministic compiler (`spec_to_report.py` → option builder) maps each whitelisted
preset onto exactly these fields. The model never writes ECharts options — it only
chooses `presetId + encoding`.

> **Pin (reproducible).** This reference is grounded in **two** official artifacts:
>
> 1. **Semantics** — the official `apache-echarts-6.1.0-src.zip` TypeScript source
>    (dropped into `references/` as the grounding for the fields listed below).
> 2. **Runtime bundle** — `assets/vendor/echarts.min.js`, the npm-built UMD bundle
>    that the report actually runs. To reproduce it exactly:
>
>    ```bash
>    curl -L -o assets/vendor/echarts.min.js \
>      https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js
>    ```
>
>    - version: **6.1.0** (`echarts.version === "6.1.0"`)
>    - size: **1,121,883 B**
>    - **sha256** `b66b25ae b4df84e3 3199dc21 694014d3 36d222cb d9deb0e5 a7c14bd6 aa0d0fd0`
>      (no spaces; the display is split only for readability)
>    - verified identical across `cdn.jsdelivr.net` and `unpkg.com` for
>      `echarts@6.1.0`; `node --check` passes.
>
> The native `apache-echarts-6.1.0-src.zip` is TypeScript source (`.ts`) and
> contains **no built `dist` browser bundle**, so it cannot be dropped into a
> `<script>` directly. The runnable bundle therefore comes from the npm package
> (mirrored by jsDelivr/unpkg). Both the semantics and the runtime now share the
> same version (`6.1.0`), so there is no semantic/runtime drift.

## Dataset transform — the official mechanism for TOP K + filtering

ECharts ships two dataset transforms in core (`src/component/transform/install.ts`):
`sort` (`sortTransform.ts`) and `filter` (`filterTransform.ts`). These are **client-
side** — they run in the browser over the dataset rows, and are the sanctioned way to
implement report filtering and TOP-K without a server.

```ts
// src/component/transform/install.ts
registers.registerTransform(filterTransform);
registers.registerTransform(sortTransform);
```

### `type: 'sort'` (src/component/transform/sortTransform.ts)

```ts
export interface SortTransformOption extends DataTransformOption {
    type: 'sort';
    config: OrderExpression | OrderExpression[];
}
type OrderExpression = { dimension: DimensionLoose; order: 'asc' | 'desc'; };
```

Documented usage:
```js
dataset: { transform: [
  { type: 'sort', config: { dimension: 'score', order: 'asc' } },
  { type: 'sort', config: [{ dimension: 1, order: 'asc' }, { dimension: 'age', order: 'desc' }] }
]}
```

### `type: 'filter'` (src/component/transform/filterTransform.ts)

```ts
export interface FilterTransformOption extends DataTransformOption {
    type: 'filter';
    config: ConditionalExpressionOption;   // { dimension, ...relation }
}
```

Conditional expressions key on a `dimension` plus a relation operator
(`gt/gte/lt/lte/eq/ne`), and can be nested. The v1 compiler emits a whitelisted subset
for the report filter bar; see `references/report-request.schema.json`.

## Series contracts we keep

| preset | viewType | series.type | fields the compiler may set | not allowed |
|---|---|---|---|---|
| bar.rank / bar.stacked | bar / stackedBar | `bar` | `name`, `type`, `data` (from packed rows), `encode`, `itemStyle`, `label` | map/graph/custom |
| line.trend / area.trend | line / area | `line`/`line`+`areaStyle` | `name`, `type`, `data`, `smooth`, `areaStyle` (area only), `encode` | custom/lines |
| scatter.correlation | scatter | `scatter` | `name`, `type`, `data` (`[x,y]`), `encode` | — |
| pie.share / donut.share | pie / donut | `pie` (donut: `radius: ['40%','70%']`) | `name`, `type`, `data:[{name,value}]`, `radius` (donut) | — |
| funnel.stage | funnel | `funnel` | `name`, `type`, `data:[{name,value}]`, `sort` | — |
| table.aggregate / table.detail | table | (HTML table; no series) | columns + rows | — |
| kpi.metric | kpi | (HTML metric card; no series) | value + kind + unit | — |

## Components we may emit

- **grid** — Cartesian axes container; `left/right/top/bottom`, `containLabel`.
- **xAxis/yAxis** — `type: 'category'|'value'|'time'`, `name`, `axisLabel` (`formatter`
  only via a safe number/date formatter, never a function string).
- **legend** — series toggling; `data`, `show`, `type: 'scroll'` for many series.
- **tooltip** — `trigger: 'axis'|'item'`, `axisPointer`.
- **dataZoom** — only for time/ordinal trends with many buckets
  (`type: 'slider'|'inside'`, `xAxisIndex`, `start`, `end`).
- **dataset** — `source`, `dimensions`, optional `transform` (sort/filter) — see above.
- **graphic / markLine / markArea** — only the static shape the compiler emits for an
  authoring override; the model never writes these.

## Strict no-go (never emit)

`pictorialBar`, `boxplot`, `treemap`, `sunburst`, `tree`, `themeRiver`, `radar`,
`effectScatter`, `candlestick`, `graph`, `sankey`, `parallel`, `map`, `heatmap`,
`custom`, `lines`. No function-string `formatter` (a lint/render-truth gate rejects
any `option` that contains a `formatter` string starting with `function` or containing
`=>`). No arbitrary backdrop/CSS or background-color editing.

## TOP K runtime control

For `bar.rank` / `bar.stacked` (and any preset with `supportsTopK: true`), a `limit`
value maps to one of two sanctioned implementations:

1. **singlefile (client-side)**: `dataset.transform = [{type:'sort',config:{dimension: measureIdx, order:'desc'}}, {type:'filter',config:{dimension: measureIdx, gte:<kthValue>}}]`
   — or a deterministic slice of the top-N rows. Runs in the browser / Web Worker.
2. **server**: `GET/POST /aggregate` with `limit` + `orderBy:[{column, direction:'desc'}]`.
   The server applies `ORDER BY ... LIMIT k`.

The model sets `limit` in the request; the runtime exposes a live TOP K control
(5 / 10 / 20 / 50 / all) that re-runs the sanctioned path. Neither path ever asks the
model for an ECharts `option`.
