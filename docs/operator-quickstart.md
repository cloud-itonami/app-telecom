# app-telecom — operator quickstart

**この文書に載っている出力・exit code は、すべてこの手順を実際に踏んで印字された
ものである。** 踏めなかった節は「踏めなかった」と書いてあり、成功と区別できる形に
してある。実施日 2026-08-18、clean clone（`git clone
https://github.com/cloud-itonami/app-telecom.git`、`48221a9`）、macOS / Python 3.14.5。

再現しない数字は引用しない。**再現するものと日ごとに変わるものを §4 で名指しする。**

---

## §1 取ってくる

```bash
git clone https://github.com/cloud-itonami/app-telecom.git
cd app-telecom
```

`48221a9eb1dbaa9e43f968e03a6c751096fb9b3f`。この repo に submodule・lockfile・
CI 設定は無い。

## §2 在庫を数える

```bash
git ls-files | while read f; do printf "%8d  %s\n" "$(wc -c <"$f")" "$f"; done
```

```
     513  NOTICE
     185  README.edn
    4811  docs/PHASE2-DESIGN.md
     397  migration.edn          ← :allowed-additions を足したので現在は 397 より大きい
    1496  worker/python/README.md
   20199  worker/python/telecom_worker.py
```

抽出時点の 4 ファイル（`NOTICE` + `docs/` + `worker/`）で **27019 バイト**。
`README.edn` と `migration.edn` が抽出時の追加物、`README.md` /
`docs/operator-quickstart.md` / `docs/verify-custody.cljs` がこの周の追加物。
どれが追加物かは `migration.edn` の `:identity :allowed-additions` が持つ。

**実行されるコードは `worker/python/telecom_worker.py`（526 行）1 本きり。**

## §3 保管を検査する

```bash
nbb docs/verify-custody.cljs              # exit 0
nbb docs/verify-custody.cljs --origin     # exit 0（gh 認証が要る）
```

`--origin` 無しで 3 検査、付けて 4 検査。実際の出力:

```
SCANNED	4 保管ファイル / 5 追加物 / 4 検査
  ok   出所 tree（再構成 vs 記録）
         got  28afef665936c0847f6ae359920755ece6548ada
  ok   保管ファイル数
         got  4
  ok   保管バイト数
         got  27019
  ok   出所 GitHub の実 tree（etzhayyim/root@691c245d:60-apps/etzhayyim-project-telecom）
         got  28afef665936c0847f6ae359920755ece6548ada
PASS — 保管対象 4 ファイルは出所と同一
```

出所 `etzhayyim/root@691c245d` の実 tree が記録と一致することは GitHub API でも
確認した（`gh api repos/etzhayyim/root/contents/60-apps?ref=691c245d…`）。

⚠ **この repo では `docs/` が保管対象と追加物の両方を含む**（`PHASE2-DESIGN.md` は
出所、他の 2 本は追加）。先行 4 repo の検査器はルート直下のエントリ単位で落として
いたので、そのままでは `docs` ごと消えて常に FAIL する。ここでは一時 index に
`read-tree` して追加パスだけを `--force-remove` し `write-tree` する。詳細は
`docs/verify-custody.cljs` 冒頭。

## §4 CLI を走らせる

```bash
cd worker/python
python3 telecom_worker.py dry-run        # exit 0
```

6 段（onboard → SIM → service → CDR → billing → SLA）を通す。DB は要らない。

**再現する値**（入力が固定なので id が入力の sha256 から決まる）:

| 段 | id |
|---|---|
| onboard | `sub_3d06a39d40790f11761295e0` |
| sim | `sim_eb96b8a1301312112678c972` |
| service | `svc_55a141c0b48dfa580857a7d8` |

**再現しない値**:

- `cdr_…` と `brc_…` / `tkt_…` — id が実行時刻から作られる。1.2 秒空けて 2 回
  走らせると 6 段中この 2 段だけが変わる（4 段は同一）。
- `inv_…` — **日付が入るので日ごとに変わる**。2026-08-18 は
  `inv_4eacdb40694e6b04c7b5d422`、8-19 は `inv_f1424f2995b0243554e3d96a`、
  8-20 は `inv_34f6a295628eb9d79d38b395`。定数として引用しないこと。

個別に呼ぶ形:

```bash
python3 telecom_worker.py activate-sim '{"iccid":"8981000123456789012","subscriberId":"sub_3d06a39d40790f11761295e0","simType":"esim"}'
```

検証に落ちると **exit 1**（実測 3 種: 必須項目欠落 / 未対応 `serviceType` /
負の `units`）。

## §5 HTTP サーバを走らせる

```bash
PORT=18080 python3 telecom_worker.py serve &
```

⚠ **起動に数秒かかる。** 負荷の高いマシンでは 2 秒後の curl が
`Connection refused` になった（4 秒後は 200）。すぐ叩いて落ちても、それは
サーバが無いことの証拠ではない。

実測した 5 経路:

| 要求 | 結果 |
|---|---|
| `GET /healthz` | 200 `{"ok":true,"runtimeKind":"k8s-langserver","agentGatewayMcpUrl":"http://agentgateway-mcp.mitama-udf.svc.cluster.local:8080"}` |
| `GET /tools` | 200、6 件（`telecom.billing.cycle` … `telecom.usage.record`） |
| `GET /nope` | 404 `{"error":"not found"}` |
| `POST` 正常 (`telecom.sim.activate`) | 200。`simId` は §4 の CLI と**同一**の `sim_eb96b8a1301312112678c972` |
| `POST` 未知の tool | 404 `{"error":"unknown tool: telecom.nope"}` |

**そして 6 経路目 —— ここが要点:**

| 要求 | 結果 |
|---|---|
| `POST` 必須項目を欠く (`subscriberId` 無し) | **応答が無い。`curl: (52) Empty reply from server`** |

`do_POST` はハンドラを try で囲んでいないので、`require()` が投げた
`ValueError: missing required field(s): subscriberId` はサーバのログに traceback として
出るだけで、**呼び出し側には HTTP status が 1 つも返らない**。同じ入力を CLI に
与えると exit 1 で理由が読める。**入力検証は存在するが、HTTP 経路では
呼び出し側から観測できない。**

`/healthz` が読み上げる `agentGatewayMcpUrl` は既定値のままである（`worker/python/README.md`
は `AGENTGATEWAY_MCP_URL=zeebe-gateway:26500` を設定するよう書いているが、この値は
どこにも接続に使われず、`/healthz` の表示専用）。

## §6 DB 経路 —— この repo だけでは踏めない

```bash
RW_URL='postgres://root@127.0.0.1:4566/dev' python3 telecom_worker.py dry-run
# psycopg.OperationalError: connection failed: … Connection refused   → exit 1
```

`psycopg` は入っていた（3.3.4）。落ちたのは接続先が無いからで、**接続できても
その先に table が無い**。この repo に DDL は 1 行も無い。

worker が要求する 7 つの relation を、実行して捕まえた（`GraphConnection` を
記録器に差し替えて `dry-run` を 1 回。dry-run 1 周で **8 文**が出る）:

| relation | 文 | 列数 |
|---|---|---|
| `vertex_telecom_subscriber` | INSERT | 14 |
| `vertex_telecom_subscriber_pii` | INSERT | 11（`sensitivity_ord` 3 = Tier-3） |
| `vertex_telecom_sim` | INSERT | 13 |
| `vertex_telecom_service` | INSERT | 16 |
| `vertex_telecom_cdr` | INSERT | 17 |
| `vertex_telecom_cdr` | SELECT（`usage_type` 別 `SUM(units)`） | 束縛 3 |
| `vertex_telecom_invoice` | INSERT | 18 |
| `vertex_telecom_sla_breach` | INSERT | 17 |

**DB 経路を実際に走らせた検証はしていない。**（この機械に Postgres /
RisingWave が無く、DDL も repo に無い。）踏めなかったことをここに書いておく。

## §7 実測で分かった 4 つのこと

すべて再現手順つき。**直していない** —— 保管対象を編集すると §3 が落ちるので、
可視化に留めた（`worker/python/telecom_worker.py` は出所のバイト列である）。

### (a) `dry-run` の請求額は常に 0.0

`fetch_cdr_aggregates` は `RW_URL` が無いと全ゼロを返す。つまり dry-run が 4 段目で
記録した 1 MiB は 5 段目の請求に載らない。**6 段が繋がって見えて、1 箇所切れている。**

集計値だけ差し込んで料金表の算術を測ると（`RATE_CARD` = voice 0.02/秒・sms 0.05/通・
data 1e-08/バイト・iot 0.001/件）:

```
data 1 MiB            -> 0.0105 JPY
voice 600 秒 + sms 10 -> 12.5   JPY
```

`RATE_CARD` の行内コメントは "Cents-per-unit" と書いているが、請求行の `currency`
既定値は `"JPY"`（補助単位を持たない）。**単位が 2 通りに読める。**

### (b) iot は請求されるのに請求行に列が無い

`total_amount` は `RATE_CARD` の 4 種すべてを合算するが、請求行が持つ列は
`voice_units` / `sms_units` / `data_units` の 3 つだけ。iot 5000 件を集計に入れると:

```
totalAmount = 5.0
記録される列 = {'voice_units': 0.0, 'sms_units': 0.0, 'data_units': 0.0}
iot_units 列は無い
```

**請求行が自分の金額を説明できない。**

### (c) 請求期間はローカル日付、監査時刻は UTC

`today_iso()` は `date.today()`（ローカル）、`now_iso()` は `datetime.now(UTC)`。
JST（+09:00）では 1 日のうち 9 時間、両者の日付が食い違う —— 例えば
`2026-08-17T22:00Z` は JST では 8-18 で、請求期間は 8-18 に、同じ行の
`created_at` は 8-17 になる。

### (d) `docs/PHASE2-DESIGN.md` は「未着手の計画」ではない

同文書が Phase 2 として**提案**する 8 本の BPMN は、抽出元の同じ revision
（`etzhayyim/root@691c245d`）に **8 本とも実在する**:

```
registerSpectrumLicense  registerCellSite      registerRanNode   registerNetworkAsset
recordSiteIncident       scheduleMaintenance   requestRma        auditPerformanceCounters
```

出所の `00-contracts/bpmn/com/etzhayyim/telecom/` には telecom BPMN が **142 本**在り、
この worker が受けるのは **6 本**。一方 worker 側の Phase 2 実装
（`kotodama.primitives.telecom_resource`）は出所にも無い —— repo 全文検索の唯一の
hit は `70-tools/config/bpmn-coverage-manifest.json` という一覧で、その一覧が名指しする
migration のパスは pin した revision で 404 になる（`30-graph/graph-schema/` 自体が
その時点で別所へ移されている）。

**「定義は在る、worker は無い」** が現在地。この文書を読んで 8 本の BPMN を
書き起こす作業を始めない。

## §8 検査が本当に落ちることを確かめた

**落ちない検査は劇場なので、5 通り壊して 5 通りとも落ちることを見た。**
すべて戻して exit 0 に復帰することも確認済み。

| 壊し方 | 結果 |
|---|---|
| 保管対象（`NOTICE`）に 1 バイト追記 | **exit 1** — 「保管バイト数」FAIL（27020 ≠ 27019） |
| 保管対象（`docs/PHASE2-DESIGN.md`）を削除して commit | **exit 1** — tree が `f3cdd5cf…` になり FAIL、ファイル数も 3 ≠ 4 |
| `:allowed-additions` に `docs/PHASE2-DESIGN.md` を紛れ込ませて迂回 | **exit 1** — 除外した分だけ再構成 tree から消えるので迂回は逆効果 |
| `migration.edn` に 2 つ目のフォームを追記 | **exit 1** — 「トップレベルのフォームが 2 個ある」 |
| repo の外（`/tmp`）から実行 | **exit 3** — UNDETERMINED（0 でも 1 でもない） |

最後の 1 行が肝心で、**「測れなかった」は「問題なし」と同じ値を返さない**。
