# telecom Phase 1 — pod-side LangServer handler

eTOM Customer + Service Provisioning core (ADR-0056 BPMN-as-actor, Wave: telecom).

## Task types

| LangServer task type | NSID | BPMN |
|---|---|---|
| `telecom.subscriber.onboard` | `com.etzhayyim.apps.telecom.onboardSubscriber` | `etzhayyim-root/00-contracts/bpmn/com/etzhayyim/telecom/onboardSubscriber.bpmn` |
| `telecom.sim.activate` | `com.etzhayyim.apps.telecom.activateSim` | `activateSim.bpmn` |
| `telecom.service.provision` | `com.etzhayyim.apps.telecom.provisionService` | `provisionService.bpmn` |
| `telecom.usage.record` | `com.etzhayyim.apps.telecom.recordUsage` | `recordUsage.bpmn` |
| `telecom.billing.cycle` | `com.etzhayyim.apps.telecom.runBillingCycle` | `runBillingCycle.bpmn` |
| `telecom.sla.escalate` | `com.etzhayyim.apps.telecom.escalateSlaBreach` | `escalateSlaBreach.bpmn` |

追加の 2 task type は `telecom.payment.record`（CLI `payment`）と
`telecom.payment.refund`（CLI `refund`）。どちらも
`com.etzhayyim.apps.telecom.payment` リソースに支払／返金を記録し、請求の
精算状態を更新する。この repo には対応する操作 NSID／BPMN の定義はない。
登録される task type は合計 8 種、`dry-run` は refund を含まない 7 段。

## Run

```bash
AGENTGATEWAY_MCP_URL=zeebe-gateway:26500 \
  RW_URL=postgres://root@45.32.79.245:4566/dev \
  python telecom_worker.py serve

# CLI smoke-test (no DB write when RW_URL unset):
python telecom_worker.py dry-run
```

## PII tier (ADR-0018)

`onboardSubscriber` writes two rows: `vertex_telecom_subscriber` (Tier-2 hashed
identity, AT Repo safe) and `vertex_telecom_subscriber_pii` (Tier-3 raw name /
MSISDN / IMSI). Downstream MVs and federation must read only the Tier-2 row.

## Phase 2 (deferred)

Resource (RAN/spectrum/inventory, TMF634/639), Supplier/Interconnect (TAP 3.12
roaming settlement), RMA / asset lifecycle.
