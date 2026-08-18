# app-telecom

**`cloud-itonami/app-telecom` は、通信事業者の eTOM Customer + Service
Provisioning を 6 つの task type として実装した Python worker 1 本と、その
設計文書を保管する repo である。**

名前が示すより狭い。ここに在るのは **worker の実装だけ**で、それを呼ぶ BPMN
定義・graph schema の DDL・デプロイ manifest は在らない（すべて出所側に残った）。
この repo だけで動かせるのは CLI と HTTP サーバであり、**データベースに書く経路は
この repo だけでは動かせない**（下記「この repo だけでは動かせないもの」）。

出所は `etzhayyim/root` の `60-apps/etzhayyim-project-telecom`。抽出時の記録は
`migration.edn`、その記録が今も正しいことは `docs/verify-custody.cljs` が検査する。

## 中身（実測 6 ファイル）

| パス | 何か |
|---|---|
| `worker/python/telecom_worker.py` | 実装の全部（526 行）。6 task type + CLI + HTTP サーバ |
| `worker/python/README.md` | 6 task type と NSID / BPMN の対応表 |
| `docs/PHASE2-DESIGN.md` | Phase 2（Resource: RAN/spectrum/inventory）の**提案**。実装は無い |
| `NOTICE` | Apache-2.0 + etzhayyim Charter Compliance Rider v3.1 |
| `README.edn` / `migration.edn` | 機械可読の repo 記録・抽出記録 |

`worker/python/telecom_worker.py` 以外に実行されるコードは無い。

## 6 task type

| task type | CLI 副コマンド | 書く table |
|---|---|---|
| `telecom.subscriber.onboard` | `onboard` | `vertex_telecom_subscriber` + `vertex_telecom_subscriber_pii` |
| `telecom.sim.activate` | `activate-sim` | `vertex_telecom_sim` |
| `telecom.service.provision` | `provision` | `vertex_telecom_service` |
| `telecom.usage.record` | `record-usage` | `vertex_telecom_cdr` |
| `telecom.billing.cycle` | `billing` | `vertex_telecom_invoice`（+ `vertex_telecom_cdr` を SELECT） |
| `telecom.sla.escalate` | `escalate` | `vertex_telecom_sla_breach` |

PII は 2 行に割れている（ADR-0018）。`vertex_telecom_subscriber` は
`msisdn_hash` / `imsi_hash` だけの Tier-2、生の氏名 / MSISDN / IMSI は
`vertex_telecom_subscriber_pii`（Tier-3、`sensitivity_ord` 3）。

## 動かす

```bash
cd worker/python
python3 telecom_worker.py dry-run          # 6 段を通す。DB 不要。exit 0
PORT=8080 python3 telecom_worker.py serve  # HTTP LangServer
```

手順と、実際に印字された値は **`docs/operator-quickstart.md`**。

## この repo だけでは動かせないもの

- **DB 書き込み。** worker は 7 つの table に INSERT / SELECT するが、その DDL は
  この repo に無い（出所側の graph-schema migration に在る）。`RW_URL` を立てれば
  psycopg で実接続を試み、table が無ければそこで落ちる。
- **BPMN 実行。** `worker/python/README.md` が挙げる 6 本の `.bpmn` は
  `etzhayyim/root` の `00-contracts/bpmn/com/etzhayyim/telecom/` に在り、ここには無い
  （抽出記録の revision で 6 本とも実在を確認済み）。
- **Zeebe。** module docstring は "Zeebe worker" と名乗るが、Zeebe client の
  コードは 1 行も無い。実体は HTTP LangServer で、`AGENTGATEWAY_MCP_URL` は
  `/healthz` が読み上げるだけで接続には使われない。

## 実測で分かった、読む前に知っておくべきこと

`docs/operator-quickstart.md` §5 に再現手順つきで書いてある。要点だけ:

1. **`dry-run` の請求額は常に 0.0。** `RW_URL` が無いと CDR 集計が全ゼロを返すので、
   同じ実行の中で記録した 1 MiB は請求に載らない。6 段が繋がって見えるが、
   5 段目だけ切れている。
2. **HTTP では入力検証が応答にならない。** 必須項目を欠いた POST は 4xx ではなく
   **接続断**（curl exit 52）になる。同じ入力を CLI に与えると exit 1。
3. **iot の使用量は請求に入るが、請求行に列が無い。** iot だけの請求は
   `total_amount` が非ゼロで `voice_units` / `sms_units` / `data_units` が全部 0 になり、
   請求行が自分の金額を説明できない。
4. **`docs/PHASE2-DESIGN.md` が「提案」する 8 本の BPMN は、抽出元の同じ revision に
   全部実在する。** この文書は未着手の計画ではなく、出所側で既に置かれた定義に対する
   凍結された提案である。出所の telecom BPMN は 142 本、この worker が受けるのは 6 本。

## 保管検査

```bash
nbb docs/verify-custody.cljs             # ローカルのみ
nbb docs/verify-custody.cljs --origin    # 出所 GitHub の実 tree とも突き合わせる
```

exit 0 = PASS / 1 = FAIL / 3 = 判定できなかった。**3 を 0 と混ぜないこと** ——
git が無い・repo の外・記録が読めない、はすべて 3 で終わる。

## ライセンス

Apache License 2.0 + etzhayyim Charter Compliance Rider v3.1。`NOTICE` を参照。
