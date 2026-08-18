#!/usr/bin/env nbb
;; verify-custody.cljs — app-telecom の保管検査
;;
;; この repo は etzhayyim/root からの抽出物である。抽出後に足された記録
;; （migration.edn の :identity :allowed-additions）を除いた残りは、出所の
;; tree と **バイト単位で同一** でなければならない。
;;
;; 検査はハッシュで行う。バイト総数の一致だけでは、足し引きが相殺する改変を
;; 通してしまう。git の tree オブジェクトを再構成して migration.edn が記録した
;; 出所 tree と突き合わせる。
;;
;;   nbb docs/verify-custody.cljs            # ローカルのみ（network 不要）
;;   nbb docs/verify-custody.cljs --origin   # 出所 GitHub の実 tree とも突き合わせる
;;
;; exit 0 = PASS / 1 = FAIL / 3 = 判定できなかった（0 でも 1 でもない）
;;
;; ⚠ 「測れなかった」を「問題なし」と同じ値で返さないこと。3 はそのための値で、
;;   git が無い・repo でない・記録が読めない・対象が 0 件、はすべて 3 で終わる。
;;
;; ⚠ tree の検査は HEAD を、バイト数の検査は working tree を見る。したがって
;;   tracked file を編集して commit していない状態は「tree ok / バイト FAIL」に
;;   なる。これは仕様で、未 commit の改変も捕まえるためにこうしてある。
;;
;; ── 出所と、先行 4 本との違い ─────────────────────────────────────────────
;;
;; この検査器は cloud-itonami/app-roukisho・app-saiban・app-shomeisyashin・app-sre の
;; docs/verify-custody.cljs と同じ役割を果たすが、**この 5 本目だけが 2 点広い**。
;; 先行 4 本にそのまま持っていける superset なので、次にあれらを触るときは
;; これを配ること（1 つだけ直る状態を避けるため）:
;;
;;   1. **追加物が保管ディレクトリの中に在ってよい。** 先行 4 本は tree を
;;      `git ls-tree HEAD`（ルート直下）から再構成し、追加物の **第 1 パス成分**を
;;      落としていた。app-telecom では `docs/` が出所側にも在る（PHASE2-DESIGN.md）
;;      ので、`docs/operator-quickstart.md` を足した瞬間に `docs` ごと落ちて
;;      **出所 tree を過小に再構成し、常に FAIL する**。ここでは一時 index に
;;      HEAD を読み込み、追加パスだけを `--force-remove` して `write-tree` する。
;;      パス単位で正確であり、working tree と本物の index には触らない。
;;   2. **migration.edn の綴りを 2 通り受ける。** 先行 4 本は
;;      `:source {:repository … :tree …}`（etzhayyim.migration/v1）。この repo は
;;      `:source {:repo … :git-tree …}`（etzhayyim.migration/extracted-v1）。
;;      綴りが違うだけで意味は同じなので、記録側ではなく検査側で吸収する。
;;      **:source の値は 1 バイトも書き換えていない。**

(require '["node:child_process" :as cp]
         '["node:fs" :as fs]
         '["node:os" :as os]
         '["node:path" :as path]
         '[clojure.edn :as edn]
         '[clojure.string :as str])

(def ^:private argv (vec (drop 2 (js->clj js/process.argv))))
(def ^:private check-origin? (some #{"--origin"} argv))

(defn- die! [code & msg]
  (binding [*print-fn* *print-err-fn*] (apply println msg))
  (js/process.exit code))

(defn- git-in [env & args]
  (try
    (str/trim (str (cp/execFileSync "git" (clj->js (vec args))
                                    #js {:encoding "utf8"
                                         :env (clj->js (merge (js->clj js/process.env) env))})))
    (catch :default e
      (die! 3 "UNDETERMINED: git" (str/join " " args) "が失敗した —"
            (or (some-> e .-message) "(理由不明)")))))

(defn- git [& args] (apply git-in {} args))

;; ── 記録を読む ────────────────────────────────────────────────────────────
;;
;; ファイル全体を [ ] で包む。包まないと edn/read-string は **先頭の 1 フォーム
;; しか読まず残りを黙って捨てる** ので、末尾に壊れたテキストを足しても素通りする。

(def ^:private record
  (let [p "migration.edn"]
    (when-not (fs/existsSync p)
      (die! 3 "UNDETERMINED:" p "が無い。この repo のルートで実行すること"))
    (let [forms (try (edn/read-string (str "[" (fs/readFileSync p "utf8") "]"))
                     (catch :default e
                       (die! 3 "UNDETERMINED:" p "が EDN として読めない —"
                             (or (some-> e .-message) ""))))]
      (when-not (= 1 (count forms))
        (die! 1 (str "FAIL: " p " のトップレベルのフォームが " (count forms)
                     " 個ある（1 個であるべき）")))
      (first forms))))

;; 綴り違いを吸収する。どちらか一方が在ればよい（両方在って食い違えば FAIL）。
(defn- one-of [m & ks]
  (let [vs (distinct (remove nil? (map #(get m %) ks)))]
    (cond (empty? vs) nil
          (= 1 (count vs)) (first vs)
          :else (die! 1 (str "FAIL: migration.edn の " (str/join " / " ks)
                             " が食い違っている: " (str/join " ≠ " vs))))))

(def ^:private src           (:source record))
(def ^:private expected-tree (one-of src :tree :git-tree))
(def ^:private expected-files (one-of src :tracked-files))
(def ^:private expected-bytes (one-of src :bytes))
(def ^:private origin-repo    (one-of src :repository :repo))
(def ^:private origin-rev     (one-of src :revision))
(def ^:private origin-path    (one-of src :path))
(def ^:private additions      (get-in record [:identity :allowed-additions]))

(doseq [[k v] {":source :tree（または :git-tree）" expected-tree
               ":source :tracked-files" expected-files
               ":source :bytes" expected-bytes
               ":identity :allowed-additions" additions}]
  (when (nil? v) (die! 3 "UNDETERMINED: migration.edn に" k "が無い")))

(when-not (and (vector? additions) (every? string? additions))
  (die! 3 "UNDETERMINED: :allowed-additions が文字列のベクタではない"))

(def ^:private addition-set (set additions))

;; ── ① 出所 tree の再構成（パス単位・一時 index） ──────────────────────────
;;
;; 保管対象のパスを :allowed-additions に紛れ込ませて検査を迂回しようとしても、
;; その分だけ再構成 tree から消えるのでハッシュが合わなくなる。

(def ^:private tracked
  (remove str/blank? (str/split-lines (git "ls-files"))))

(when (zero? (count tracked))
  (die! 3 "UNDETERMINED: git ls-files が空。commit の無い repo か"))

(let [missing (remove (set tracked) additions)]
  (when (seq missing)
    (die! 3 "UNDETERMINED: :allowed-additions が tracked でないパスを挙げている:"
          (str/join " " missing))))

(def ^:private preserved-files (remove addition-set tracked))

(when (zero? (count preserved-files))
  (die! 3 "UNDETERMINED: 保管対象のファイルが 0 件。"
        ":allowed-additions が全てを覆っている"))

(def ^:private actual-tree
  (let [idx (path/join (os/tmpdir) (str "verify-custody-" (js/process.pid) ".idx"))
        env {"GIT_INDEX_FILE" idx}]
    (try
      (when (fs/existsSync idx) (fs/unlinkSync idx))
      (git-in env "read-tree" "HEAD")
      (doseq [p additions] (git-in env "update-index" "--force-remove" p))
      (git-in env "write-tree")
      (finally
        (when (fs/existsSync idx) (fs/unlinkSync idx))))))

;; ── ② 保管対象の実ファイル数とバイト数 ────────────────────────────────────

(def ^:private actual-bytes
  (reduce + 0 (map (fn [p]
                     (if (fs/existsSync p)
                       (.-size (fs/statSync p))
                       (die! 3 "UNDETERMINED: tracked なのに実体が無い:" p)))
                   preserved-files)))

;; ── 判定 ──────────────────────────────────────────────────────────────────

(def ^:private results
  (cond-> [{:what "出所 tree（再構成 vs 記録）" :ok (= actual-tree expected-tree)
            :got actual-tree :want expected-tree}
           {:what "保管ファイル数" :ok (= (count preserved-files) expected-files)
            :got (count preserved-files) :want expected-files}
           {:what "保管バイト数" :ok (= actual-bytes expected-bytes)
            :got actual-bytes :want expected-bytes}]
    check-origin?
    (conj (if (some nil? [origin-repo origin-rev origin-path])
            (die! 3 "UNDETERMINED: --origin には :source の"
                  ":repository（または :repo）/ :revision / :path が要る")
            (let [parent (str/join "/" (butlast (str/split origin-path #"/")))
                  leaf   (last (str/split origin-path #"/"))
                  out    (try
                           (str (cp/execFileSync
                                 "gh" (clj->js ["api" (str "repos/" origin-repo "/contents/"
                                                           parent "?ref=" origin-rev)
                                                "--jq" (str ".[] | select(.name==\"" leaf "\") | .sha")])
                                 #js {:encoding "utf8"}))
                           (catch :default e
                             (die! 3 "UNDETERMINED: 出所 GitHub を引けなかった（gh 未認証/圏外?）—"
                                   (or (some-> e .-message) ""))))
                  sha (str/trim out)]
              (when (str/blank? sha)
                (die! 3 "UNDETERMINED: 出所に" origin-path "が見つからない"))
              {:what (str "出所 GitHub の実 tree（" origin-repo "@" (subs origin-rev 0 8)
                          ":" origin-path "）")
               :ok (= sha expected-tree) :got sha :want expected-tree}))))))

(println (str "SCANNED\t" (count preserved-files) " 保管ファイル / "
              (count additions) " 追加物 / " (count results) " 検査"))
(doseq [{:keys [what ok got want]} results]
  (println (str (if ok "  ok   " "  FAIL ") what))
  (println (str "         got  " got))
  (when-not ok (println (str "         want " want))))

(if (every? :ok results)
  (do (println (str "PASS — 保管対象 " (count preserved-files) " ファイルは出所と同一"
                    (when-not check-origin? "（--origin を付けると出所 GitHub とも突き合わせる）")))
      (js/process.exit 0))
  (do (println "FAIL — 保管対象が出所と一致しない")
      (js/process.exit 1)))
