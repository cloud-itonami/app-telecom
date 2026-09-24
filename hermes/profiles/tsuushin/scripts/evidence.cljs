;; tsuushin evidence script — cron agent はこれを実行して出力 JSON を読んで報告するだけ。
;; 判断・計算はこの script が持つ。credential を読まない。
;; 実行: nbb ~/.hermes/profiles/tsuushin/scripts/evidence.cljs

(require '[clojure.string :as str]
         '["node:child_process" :as cp]
         '["node:fs" :as fs]
         '["node:path" :as path]
         '["node:os" :as os])

(def repo-root (path/join (os/homedir) "github" "com-junkawasaki"))
(def out-dir (path/join (os/homedir) ".hermes" "profiles" "tsuushin" "workspace" "findings"))

(def repos
  ["orgs/cloud-itonami/app-telecom"
   "orgs/kotoba-lang/phone"
   "orgs/kotoba-lang/org-ietf-sip"
   "orgs/kotoba-lang/kami-app-sip"
   "orgs/cloud-itonami/cloud-itonami-numbering"
   "orgs/kotoba-lang/koe"
   "orgs/kotoba-lang/com-twilio"
   "orgs/kotoba-lang/com-line-messaging"
   "orgs/kotoba-lang/rcs"
   "orgs/kotoba-lang/esim"
   "orgs/cloud-itonami/actor-iriai"])

(defn sh [cmd cwd]
  (try (str/trim (str (cp/execSync cmd #js {:cwd cwd :timeout 15000})))
       (catch :default e (str "ERR:" (.-message e)))))

(defn repo-probe [rel]
  (let [abs (path/join repo-root rel)]
    (if (fs/existsSync abs)
      {:path rel
       :head (subs (sh "git rev-parse HEAD" abs) 0 12)
       :dirty? (not (str/blank? (sh "git status --porcelain" abs)))
       :has-test? (some #(fs/existsSync (path/join abs %))
                        ["src/test" "test" "worker"])}
      {:path rel :head "NOT-CHECKED-OUT" :dirty? false :has-test? false})))

(defn main []
  (let [ts (.toISOString (js/Date.))
        repos-data (mapv repo-probe repos)
        result {:at ts
                :kind "tsuushin-evidence"
                :repos repos-data
                :priority-order ["etom-billing-charging" "carrier-adapter"
                                 "voice-runtime" "sms-mail-runner"
                                 "telecom-business-law-blueprint"]}]
    (fs/mkdirSync out-dir #js {:recursive true})
    (fs/writeFileSync (path/join out-dir (str "tsuushin-evidence-"
                                              (.slice ts 0 10) ".json"))
                      (js/JSON.stringify (clj->js result) nil 2))
    (println (js/JSON.stringify (clj->js result) nil 2))))

(main)
