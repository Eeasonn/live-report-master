# Usability & capability audit (multi-table editor surface)

A systematic review of what the multi-table universe + editor actually expose
to a user, and what is still missing. Each finding cites the code location.
Status: `[fixed]` = implemented in `assets/runtime/editor-runtime.js` (+CSS);
`[deferred]` = documented but not yet implemented (no feature expansion).

## A. Multi-table capabilities are under-exposed in the UI

1. **[fixed] New charts/KPIs/details always bind to the first table.**
   `addBI()` / `addValue()` / `addDetail()` hard-code `(manifest.datasets||[])[0]`
   (`editor-runtime.js` around `addBI`/`addValue`/`addDetail`). With 3 tables a
   user could only ever chart `sales`. Now: when >1 dataset, a **数据源 picker**
   opens first (list of tables with column/row counts); single-table reports keep
   the one-click flow.

2. **[fixed] A chart can never be re-bound to another table.**
   The chart settings panel (`openChart`) had no dataset control — the dataset was
   fixed at creation. Now a **数据源** select appears at the top of the panel
   (only when >1 dataset); switching it resets dimension/measure to the new
   table's sensible defaults and re-renders.

3. **[fixed] Cards do not label their data source.**
   Nothing on a chart card says which table it aggregates. Now every chart card
   carries a `数据源：<id>（<label>）` chip next to its title whenever >1 dataset
   exists (visible in read AND edit mode).

4. **[fixed] 更新数据 requires typing 0/1/2/3 into a prompt.**
   The `update-data` handler used `window.prompt('…输入序号或 id…')`
   (`editor-runtime.js` `update-data` action). Now it opens a **picker panel**
   listing the tables (id + label + 列/行 counts) plus a `＋ 新增数据源（追加）`
   button — no typing.

## B. Capability hints & help text are stale / absent

5. **[fixed] The 帮助 panel is outdated.**
   `helpGuideHtml()` only mentioned "同结构的 Excel/JSON" and said nothing about
   CSV/TSV, multi-source append, cross-table alignment, or dataset switching.
   Rewritten with a **多数据源与更新** section + a **已知限制** section.

6. **[fixed] No discoverable capability entry point.**
   Multi-source features lived only inside the advanced 数据配置 canvas. The help
   panel + update-data picker now surface them; the semantic canvas keeps the deep
   controls (dataset switcher + 跨表对齐 panel).

## C. Known limitations that were never stated

7. **[fixed → documented in help]** New data sources must be **2-D rectangular**
   tables (first row = header). Server mode enforces this in `app.py`
   ("dataset rows must be rectangular"; upload cap 5,000,000 rows).

8. **[fixed → documented in help]** Data-volume limits:
   - **Singlefile** packages rows inline; auto-mode flips to server mode above
     `singlefileMaxRows` (default **100,000 rows**, `spec_to_report.py`
     `--threshold` / `singlefileMaxRows`).
   - **Server upload** accepts at most **5,000,000 rows** per dataset
     (`server/app.py` dataset register guard).

9. **[fixed → documented in help]** Replacing an existing table (更新历史数据)
   requires the **same column structure** (same column names → row-for-row
   replace). A different structure is treated as a **new data source** (append),
   never silently merged.

## D. Mode parity (singlefile vs server)

Feature parity was checked; the answer is: **same feature surface, different
storage/mutation model** — NOT only a data-volume difference.

- **[fixed] server template was missing 2 runtime assets**: `column-matcher.js`
  and `xlsx-reader.js` were embedded in singlefile (`package_report.py`) but not
  in `server/template.py`, so server reports failed on `.xlsx`/`.csv`/`.tsv`
  import (`AIReportReadXlsx` undefined) and on the different-schema column-mapping
  wizard (`AIReportColumnMatch` undefined). Both are now embedded.
- **[documented] server-mode runtime data mutation is session-local**: 更新数据
  (replace) mutates the client's in-memory `dataset.rows` and 追加 adds a
  client-only dataset; the server's SQLite store is NOT updated, and a
  client-appended table has no server aggregate endpoint. The UI now shows a
  warning in the 更新数据 picker. Persistent server data changes need the
  report-build pipeline (or a future `POST /api/datasets` register/reload).
- **Data volume** is the intended difference: singlefile inline rows
  (≤ ~100k, `singlefileMaxRows`), server external SQLite (≤ 5M rows/table).

## E. Deletion (necessary symmetric operation)

- **[fixed] delete dataset**: previously append/update existed but there was NO
  way to remove a table. Added `removeDataset` + a 删除当前表 button in the
  数据配置 canvas toolbar (multi-table only). Deleting a table also removes the
  charts bound to it and any cross-table relation touching it; it re-derives
  `dimensionKeys` and re-opens the canvas. At least one table is always kept.
  KPI/detail cards carry no `datasetId` (they follow the first table), so they
  re-bind to the new first table after a delete — documented in the confirm.

## Deferred (deliberately not expanded)

- Server-mode runtime append: appending a table to an **already-registered
  server report** needs a `POST /api/datasets` register/reload endpoint. The
  client-side append path exists (singlefile); server append is out of scope here.
- KPI / detail cards do not yet show a data-source chip (charts do). They are
  bound to the first dataset by construction; adding chips there is cosmetic and
  can follow the same pattern when needed.
