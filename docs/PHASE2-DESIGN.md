# telecom Phase 2 — Resource domain (RAN / spectrum / inventory)

Phase 1 (eTOM Customer + Service Provisioning) live: 6 BPMN actors covering
subscriber / SIM / service / CDR / billing / SLA. Phase 2 closes the
**Resource** quadrant of eTOM with TMF634 (Resource Catalog) / TMF639
(Resource Inventory) / TMF671 (Performance Management) shapes.

## Phase 2 BPMN actors (proposed)

| BPMN | NSID | task type | 用途 |
|---|---|---|---|
| `registerSpectrumLicense` | `com.etzhayyim.apps.telecom.registerSpectrumLicense` | `telecom.spectrum.register` | 周波数免許 (band / MHz / region / 期限) |
| `registerCellSite` | `com.etzhayyim.apps.telecom.registerCellSite` | `telecom.site.register` | 基地局 (lat/lon / tower owner / power) |
| `registerRanNode` | `com.etzhayyim.apps.telecom.registerRanNode` | `telecom.ranNode.register` | gNB/eNB/DU/CU + spectrum binding |
| `registerNetworkAsset` | `com.etzhayyim.apps.telecom.registerNetworkAsset` | `telecom.asset.register` | 物理 asset (router / fiber / antenna) serial# tracking |
| `recordSiteIncident` | `com.etzhayyim.apps.telecom.recordSiteIncident` | `telecom.site.incident` | 障害 (outage / degradation / vandalism) |
| `scheduleMaintenance` | `com.etzhayyim.apps.telecom.scheduleMaintenance` | `telecom.maintenance.schedule` | 保全計画 (preventive / corrective / spec window) |
| `requestRma` | `com.etzhayyim.apps.telecom.requestRma` | `telecom.rma.request` | 故障返品 (asset → vendor RMA case) |
| `auditPerformanceCounters` | `com.etzhayyim.apps.telecom.auditPerformanceCounters` | `telecom.kpi.audit` | TMF671 PM (RRC drop / DL throughput / handover / CQI) |

Timer-start で `auditPerformanceCounters` を `R/PT15M` (15 分間隔) にすると、
`escalateSlaBreach` (Phase 1) に自動連鎖して Customer 側 SLA 通知が走る。

## 永続化 (graph schema)

| vertex | tier | 主キー候補 |
|---|---|---|
| `vertex_telecom_spectrum_license` | C | `(jurisdiction, band, license_id)` |
| `vertex_telecom_cell_site` | C | `site_id` (E-UTRAN/NR cell global ID 由来) |
| `vertex_telecom_ran_node` | C | `node_id` (gNB/eNB ID) |
| `vertex_telecom_network_asset` | C | `serial_number` |
| `vertex_telecom_site_incident` | C | `incident_id` |
| `vertex_telecom_maintenance_window` | C | `window_id` |
| `vertex_telecom_rma_case` | C | `rma_id` |
| `vertex_telecom_kpi_sample` | C (high cardinality, monthly partition 候補) | `(node_vid, metric, sampled_at)` |

| edge | 用途 |
|---|---|
| `edge_telecom_site_hosts_node` | site → ran_node |
| `edge_telecom_node_uses_spectrum` | ran_node → spectrum_license |
| `edge_telecom_asset_installed_at` | asset → site / node |
| `edge_telecom_service_runs_on_node` | Phase 1 service → ran_node (capacity ↔ subscriber) |
| `edge_telecom_incident_affects_service` | incident → Phase 1 service (auto SLA breach) |

MV:

- `mv_telecom_site_health` — site × node × open incidents × open maintenance
- `mv_telecom_spectrum_utilization` — license × bound nodes × subscriber 数
- `mv_telecom_kpi_p95_15m` — node × metric の 15 分 p95 (streaming MV; ADR-0044 SQL UDF)

## Phase 2 が利用する既存 generic primitives

ADR-0056 規約により `generic.*` で済むものは新規 task 不要:

- `generic.db.select` — KPI threshold 取得、RMA 状態確認
- `generic.db.insert` — `vertex_telecom_kpi_sample` 大量投入時 (worker 経由より速い場合)
- `generic.http.fetch` — vendor portal RMA 状態 polling
- `generic.audit.emit` — 全 BPMN の末尾 audit (Phase 1 と同じ)

新規必要な primitive は **Resource 8 task type のみ**。Customer + Service と
合わせて telecom actor 全体で **14 task type / 14 BPMN**。

## ERC-8004 agent registration への影響

新規 token は不要。既存の `did:web:telecom.etzhayyim.com` agent registration の
`protocols[].mcp.tools[]` と `collections[]` に Phase 2 の 8 NSID を追加して
再 publish (`etzhayyim agent-runtime publish-agent`) するだけ。`registries.toolRegistryRows[]`
にも 8 行追加。

## Phase 3+ scope-out

- **Supplier/Interconnect** (TAP 3.12 roaming settlement / MNO-MVNO
  wholesale) — ADR 別建て
- **5G Core SBA 統合** — AMF/SMF/UDM のラッパー BPMN
- **Number portability** (MNP) — 規制側プロセス (キャリア間清算)
- **Lawful Intercept (CALEA)** — 強い PII / Tier-3 + audit-only

## 投入順序 (推奨)

1. graph schema migration (vertex + edge + MV) — 8 vertex / 5 edge / 3 MV
2. 8 BPMN + 8 lexicon JSON
3. `kotodama.primitives.telecom_resource` モジュール追加 (Phase 1 と分離、import lazy)
4. `zeebe_worker_main.py` に `telecom_resource.register(worker, ...)` 追記
5. ERC-8004 registration JSON 更新 → `publish-agent` で IPFS 再 pin (onchain 再登録は agentURI 変更時のみ)
