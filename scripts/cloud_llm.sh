#!/usr/bin/env bash
# cloud_llm.sh — 在雲端 GPU（Vast.ai 或 RunPod）開一個 vLLM 或 SGLang 伺服器，
# 讓 book-translator 的 omlx-compatible client 直接打它，翻完砍機。
#
# 跟 bookcast／srt 的雲端腳本不同：這裡不上傳程式、不走 SSH。翻譯器本來就只對一個 OpenAI 相容
# 網址發請求（本機 omlx 在 :8090），所以雲端版只要「租一台、用官方 server 映像起服務、
# 對外開 8000 埠、拿到公網位址就當 --omlx-host」。金鑰用 server 的 --api-key 擋公開埠。
#
# 用法：
#   scripts/cloud_llm.sh [選項] -- <translate_book_ollama.py 的參數...>
#   scripts/cloud_llm.sh --provider vast -- --book X.epub --out translations/
#   scripts/cloud_llm.sh --profile fp8 -- --book X.epub           # 這本要更好：FP8 + 48GB 卡
#   scripts/cloud_llm.sh --keep -- --book A.epub                    # 翻完不砍，端點寫進 runs/<id>/endpoint.env
#   scripts/cloud_llm.sh --stop runs/cloud-llm-XXXX                 # 砍掉 --keep 留下的那台
#
# 選項（都有對應環境變數）：
#   --provider vast|runpod   CLOUD_LLM_PROVIDER（預設 vast）
#   --profile int4|fp8       CLOUD_LLM_PROFILE（預設 int4）：
#                              int4 = XReyRobert/Qwopus3.6-27B-v2-GPTQ-Pro-v1（18.7 GB，跟本機 MLX 4bit 同級，5090 塞得下）
#                              fp8  = Jackrong/Qwopus3.6-27B-v2-FP8（30.9 GB，要 ≥40 GB 卡，預設 L40S）
#   --model HF_ID            CLOUD_LLM_MODEL（蓋掉 profile 的模型）
#   --gpu NAME               CLOUD_LLM_GPU（蓋掉 profile 的卡；Vast 寫法 "RTX 5090"，RunPod 寫法 "NVIDIA GeForce RTX 5090"）
#   --max-dph X              CLOUD_LLM_MAX_DPH（Vast 價格上限，預設 int4 0.6 / fp8 1.2）
#   CLOUD_LLM_ENGINE          vllm|sglang（預設 vllm）
#   CLOUD_LLM_CONCURRENCY     client 併發寬度；未明傳時由就緒後的真實大塊探針自動選 24/16/12/8
#   CLOUD_LLM_CONCURRENT_CHAPTERS=0  關掉雲端預設的跨章併發，退回舊行為
#   CLOUD_LLM_EXTRA_SERVER_ARGS  追加目前引擎的 server 參數；vllm 仍相容舊的 CLOUD_LLM_EXTRA_VLLM_ARGS
#   --keep / --stop DIR      見上
#
# 憑證：Vast → VAST_API_KEY 或 ~/.config/vastai/vast_api_key；RunPod → RUNPOD_API_KEY 或 ~/.config/runpod/api_key。
# 機器生命週期用 srt-skill 的共用檔（BOOK_TRANSLATOR_CLOUD_LIB_DIR 可改，預設 ~/dev/srt-skill/scripts）。
#
# 錢的守衛（跟 bookcast 同一套精神，寫在程式裡不靠人記得）：
#   - 任何退出路徑都砍機（trap），砍完回查清單確認消失；砍不掉就大聲說、留下 id 檔
#   - 開機前先算好上限：CLOUD_LLM_BOOT_WAIT_MIN（預設 25，拉 10 GB 映像＋下載 19–31 GB 模型）內
#     伺服器沒起來就砍；Vast 死狀態／拉映像停滯也砍
#   - 翻譯本身有 CLOUD_LLM_MAX_HOURS（預設 6）的看門狗：超時送 TERM 給翻譯器，trap 接手砍機

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LIB_DIR="${BOOK_TRANSLATOR_CLOUD_LIB_DIR:-$HOME/dev/srt-skill/scripts}"

PROVIDER="${CLOUD_LLM_PROVIDER:-vast}"
PROFILE="${CLOUD_LLM_PROFILE:-int4}"
ENGINE="${CLOUD_LLM_ENGINE:-vllm}"
MODEL="${CLOUD_LLM_MODEL:-}"
GPU="${CLOUD_LLM_GPU:-}"
MAX_DPH="${CLOUD_LLM_MAX_DPH:-}"
# 16 是探針本身失敗時的全域 fallback。明傳值仍會跑探針留下證據，但 client 永遠尊重明傳值。
if [[ -n "${CLOUD_LLM_CONCURRENCY+x}" ]]; then
    CONCURRENCY="$CLOUD_LLM_CONCURRENCY"
    CONCURRENCY_EXPLICIT=true
else
    CONCURRENCY=16
    CONCURRENCY_EXPLICIT=false
fi
# Server 必須容納最高候選，否則用 16 開機送 24 筆只會量到 server queue，不是真 N=24。
SERVER_CONCURRENCY=24
CONCURRENT_CHAPTERS="${CLOUD_LLM_CONCURRENT_CHAPTERS:-1}"
KEEP=false
STOP_DIR=""
TRANSLATE_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider) PROVIDER="$2"; shift 2 ;;
        --profile)  PROFILE="$2"; shift 2 ;;
        --model)    MODEL="$2"; shift 2 ;;
        --gpu)      GPU="$2"; shift 2 ;;
        --max-dph)  MAX_DPH="$2"; shift 2 ;;
        --keep)     KEEP=true; shift ;;
        --stop)     STOP_DIR="$2"; shift 2 ;;
        --help|-h)  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --)         shift; TRANSLATE_ARGS=("$@"); break ;;
        *)          echo "不認識的選項：$1（翻譯器的參數要放在 -- 後面）" >&2; exit 2 ;;
    esac
done

log()  { printf '[cloud-llm] %s\n' "$*" >&2; }
die()  { printf '[cloud-llm] 失敗：%s\n' "$*" >&2; exit 1; }

case "$PROVIDER" in vast|runpod) ;; *) die "--provider 只能是 vast 或 runpod：$PROVIDER" ;; esac
case "$ENGINE" in vllm|sglang) ;; *) die "CLOUD_LLM_ENGINE 只能是 vllm 或 sglang：$ENGINE" ;; esac
[[ "$CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || die "CLOUD_LLM_CONCURRENCY 必須是正整數：$CONCURRENCY"
if [[ "$CONCURRENCY_EXPLICIT" == true && "$CONCURRENCY" -gt "$SERVER_CONCURRENCY" ]]; then
    SERVER_CONCURRENCY="$CONCURRENCY"
fi
case "$CONCURRENT_CHAPTERS" in 0|1) ;; *) die "CLOUD_LLM_CONCURRENT_CHAPTERS 只能是 0 或 1：$CONCURRENT_CHAPTERS" ;; esac
case "$PROFILE" in
    int4)
        MODEL="${MODEL:-XReyRobert/Qwopus3.6-27B-v2-GPTQ-Pro-v1}"
        [[ "$PROVIDER" == vast ]] && GPU="${GPU:-RTX 5090}" || GPU="${GPU:-NVIDIA GeForce RTX 5090}"
        MAX_DPH="${MAX_DPH:-0.6}" ;;
    fp8)
        MODEL="${MODEL:-Jackrong/Qwopus3.6-27B-v2-FP8}"
        [[ "$PROVIDER" == vast ]] && GPU="${GPU:-L40S}" || GPU="${GPU:-NVIDIA L40S}"
        MAX_DPH="${MAX_DPH:-1.2}" ;;
    *) die "--profile 只能是 int4 或 fp8：$PROFILE" ;;
esac
if [[ "$ENGINE" == vllm ]]; then
    IMAGE="${CLOUD_LLM_IMAGE:-vllm/vllm-openai:v0.28.0}"
else
    IMAGE="${CLOUD_LLM_IMAGE:-lmsysorg/sglang:latest-runtime}"
fi
# 挑報價用預估總費用排序：這條流程流量最重——每次拉 10 GB 映像＋ int4 19 GB／fp8 31 GB 模型，
# 流量費常高過 GPU 費（Vast 單價 0 到 0.039 美元／GB）。時數預設 1.5 小時（一本 47 萬字約 1 小時），可用環境變數改。
export VAST_LIB_EST_HOURS="${VAST_LIB_EST_HOURS:-1.5}"
export VAST_LIB_EST_DOWN_GB="${VAST_LIB_EST_DOWN_GB:-$([[ "$PROFILE" == fp8 ]] && echo 41 || echo 29)}"
export VAST_LIB_EST_UP_GB="${VAST_LIB_EST_UP_GB:-0.1}"
DISK_GB="${CLOUD_LLM_DISK_GB:-80}"
PORT=8000
MAX_MODEL_LEN="${CLOUD_LLM_MAX_MODEL_LEN:-16384}"
GPU_MEM_UTIL="${CLOUD_LLM_GPU_MEM_UTIL:-0.92}"
BOOT_WAIT_MIN="${CLOUD_LLM_BOOT_WAIT_MIN:-25}"
BOOT_STALL_MIN="${CLOUD_LLM_BOOT_STALL_MIN:-6}"
MAX_HOURS="${CLOUD_LLM_MAX_HOURS:-6}"
POLL_SECONDS="${CLOUD_LLM_POLL_SECONDS:-20}"
# 測試鉤子：source 一個檔，讓測試替換 CLI／transport，並用 CLOUD_LLM_TEST_TRANSLATE_CMD 取代翻譯器
if [[ -n "${CLOUD_LLM_TEST_HOOK_FILE:-}" ]]; then
    # shellcheck source=/dev/null
    source "$CLOUD_LLM_TEST_HOOK_FILE"
fi

# ---------- 共用檔 ----------
if [[ "$PROVIDER" == vast ]]; then
    [[ -r "$LIB_DIR/vast_instance_lib.sh" ]] || die "找不到 $LIB_DIR/vast_instance_lib.sh（設 BOOK_TRANSLATOR_CLOUD_LIB_DIR）"
    # shellcheck source=/dev/null
    source "$LIB_DIR/vast_instance_lib.sh"
    vast_lib_info() { log "$*"; }; vast_lib_error() { log "⚠️  $*"; }; vast_lib_die() { die "$*"; }
else
    [[ -r "$LIB_DIR/runpod_pod_lib.sh" ]] || die "找不到 $LIB_DIR/runpod_pod_lib.sh（設 BOOK_TRANSLATOR_CLOUD_LIB_DIR）"
    # shellcheck source=/dev/null
    source "$LIB_DIR/runpod_pod_lib.sh"
    runpod_lib_info() { log "$*"; }; runpod_lib_error() { log "⚠️  $*"; }; runpod_lib_die() { die "$*"; }
fi
if [[ -n "${CLOUD_LLM_TEST_HOOK_FILE:-}" ]] && declare -F cloud_llm_test_after_libs >/dev/null; then
    cloud_llm_test_after_libs
fi

# ---------- --stop：砍掉 --keep 留下的那台 ----------
INSTANCE_ID=""
if [[ -n "$STOP_DIR" ]]; then
    [[ -f "$STOP_DIR/instance.id" ]] || die "找不到 $STOP_DIR/instance.id"
    INSTANCE_ID="$(tr -d '[:space:]' <"$STOP_DIR/instance.id")"
    PROVIDER="$(tr -d '[:space:]' <"$STOP_DIR/provider" 2>/dev/null || echo "$PROVIDER")"
fi

# ---------- 砍機（任何退出路徑） ----------
RUN_DIR=""
TERMINATED=false
terminate_instance() {
    [[ -n "$INSTANCE_ID" ]] || return 0
    [[ "$TERMINATED" == true ]] && return 0
    if [[ "$KEEP" == true && "${KEEP_ACTIVE:-false}" == true ]]; then
        log "--keep：機器 $INSTANCE_ID 留著（GPU 持續計費）。砍：$0 --stop $RUN_DIR"
        return 0
    fi
    log "砍機 $INSTANCE_ID"
    local rc=0 err=""
    if [[ "$PROVIDER" == vast ]]; then
        vast_lib_terminate_instance_once "$INSTANCE_ID" || rc=$?; err="$VAST_LIB_TERMINATE_LAST_ERROR"
    else
        runpod_lib_terminate_pod_once "$INSTANCE_ID" || rc=$?; err="$RUNPOD_LIB_TERMINATE_LAST_ERROR"
    fi
    if [[ $rc -eq 0 ]]; then
        TERMINATED=true; log "已確認 $INSTANCE_ID 不在清單中，計費停止"
        [[ -n "$RUN_DIR" ]] && rm -f "$RUN_DIR/instance.id"
        return 0
    fi
    printf '[cloud-llm] ⚠️  砍機後 %s 仍在清單（%s）。id 留在 %s；請手動確認：%s\n' \
        "$INSTANCE_ID" "${err:-unknown}" "${RUN_DIR:-?}/instance.id" \
        "$([[ "$PROVIDER" == vast ]] && echo 'bash ~/dev/srt-skill/scripts/vast_reap.sh' || echo 'bash ~/dev/srt-skill/scripts/runpod_reap.sh')" >&2
    return 1
}
trap 'rc=$?; terminate_instance || true; exit "$rc"' EXIT
trap 'terminate_instance || true; exit 130' INT
trap 'terminate_instance || true; exit 143' TERM
trap 'terminate_instance || true; exit 129' HUP

if [[ -n "$STOP_DIR" ]]; then
    RUN_DIR="$STOP_DIR"; KEEP=false
    terminate_instance && exit 0
    exit 1
fi

# ---------- 前置檢查（開機前能擋的都在這裡擋） ----------
[[ ${#TRANSLATE_ARGS[@]} -gt 0 ]] || die "缺翻譯器參數：在 -- 後面給 translate_book_ollama.py 的參數（至少 --book）"
for tool in jq curl python3; do command -v "$tool" >/dev/null || die "需要 $tool"; done
if [[ "$PROVIDER" == vast ]]; then
    command -v "${VAST_LIB_CLI:-vastai}" >/dev/null || die "Vast.ai 需要 vastai CLI（pip install vastai）"
    vast_lib_load_api_key || die "找不到 Vast.ai 金鑰：設 VAST_API_KEY 或建立 $(vast_lib_api_key_file_hint)"
else
    runpod_lib_load_api_key || die "找不到 RunPod 金鑰：設 RUNPOD_API_KEY 或建立 $(runpod_lib_api_key_file_hint)"
fi
[[ -x "$REPO_ROOT/scripts/translate_book_ollama.py" || -f "$REPO_ROOT/scripts/translate_book_ollama.py" ]] || die "找不到 translate_book_ollama.py"

RUN_DIR="$REPO_ROOT/runs/cloud-llm-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$RUN_DIR"
printf '%s\n' "$PROVIDER" >"$RUN_DIR/provider"
# 在開始計費前，從本次要翻的第一本書建立 60 個 production-size prompt。正常長書四波
# 互不重複；短書不得假裝有足量，builder 會重複並把事實記在 load.json。
PROBE_BOOK=""
_expect_book=false
for _arg in "${TRANSLATE_ARGS[@]}"; do
    if [[ "$_expect_book" == true ]]; then PROBE_BOOK="$_arg"; break; fi
    if [[ "$_arg" == --book ]]; then _expect_book=true
    elif [[ "$_arg" == --book=* ]]; then PROBE_BOOK="${_arg#--book=}"; break
    fi
done
[[ -n "$PROBE_BOOK" ]] || die "找不到 --book，無法建立自適應併發 load.json"
# 固定 load 只能給不租機的測試替身；正式流程永遠取本次 EPUB，避免 stale load 誤導探針。
if [[ -n "${CLOUD_LLM_TEST_PROBE_CMD:-}" && -n "${CLOUD_LLM_PROBE_LOAD:-}" ]]; then
    PROBE_LOAD="$CLOUD_LLM_PROBE_LOAD"
    [[ -r "$PROBE_LOAD" ]] || die "讀不到測試用 CLOUD_LLM_PROBE_LOAD：$PROBE_LOAD"
else
    PROBE_LOAD="$RUN_DIR/load.json"
    if [[ -n "${CLOUD_LLM_TEST_BUILD_LOAD_CMD:-}" ]]; then
        read -r -a BUILD_LOAD_CMD <<<"$CLOUD_LLM_TEST_BUILD_LOAD_CMD"
    else
        BUILD_LOAD_CMD=(python3 "$SCRIPT_DIR/build_adaptive_probe_load.py")
    fi
    "${BUILD_LOAD_CMD[@]}" --book "$PROBE_BOOK" --out "$PROBE_LOAD" \
        || die "無法從 $PROBE_BOOK 建立自適應併發 load.json（尚未租機）"
fi
API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
LABEL="book-translator-$(date -u +%Y%m%dT%H%M%SZ)"
log "平台 $PROVIDER / 引擎 $ENGINE / 卡 $GPU / 模型 $MODEL / 映像 $IMAGE / run $RUN_DIR"

# 每個映像各自組合法合法的啟動參數，不能靠 append 企圖蓋掉另一個引擎的旗標。
if [[ "$ENGINE" == vllm ]]; then
    # vLLM 映像的 entrypoint 已是 `vllm serve`；模型是位置參數。
    SERVER_ARGS=("$MODEL" --served-model-name "$MODEL" --port "$PORT" --host 0.0.0.0
                 --api-key "$API_KEY" --max-model-len "$MAX_MODEL_LEN" --gpu-memory-utilization "$GPU_MEM_UTIL"
                 --max-num-seqs "$SERVER_CONCURRENCY")
else
    # SGLang runtime 映像沒有 vLLM entrypoint；明確啟動 OpenAI-compatible server。
    # reasoning parser 是品質守衛的一部分，不能只靠 request 的 enable_thinking=false。
    SERVER_ARGS=(python3 -m sglang.launch_server --model-path "$MODEL" --port "$PORT" --host 0.0.0.0
                 --api-key "$API_KEY" --context-length "$MAX_MODEL_LEN" --mem-fraction-static "$GPU_MEM_UTIL"
                 --max-running-requests "$SERVER_CONCURRENCY" --reasoning-parser qwen3)
fi
if [[ -n "${CLOUD_LLM_EXTRA_SERVER_ARGS:-}" ]]; then
    read -r -a _extra <<<"$CLOUD_LLM_EXTRA_SERVER_ARGS"
elif [[ "$ENGINE" == vllm && -n "${CLOUD_LLM_EXTRA_VLLM_ARGS:-}" ]]; then
    # Backward compatibility for existing vLLM benchmark commands only.
    read -r -a _extra <<<"$CLOUD_LLM_EXTRA_VLLM_ARGS"
else
    _extra=()
fi
for _arg in ${_extra[@]+"${_extra[@]}"}; do
    case "$_arg" in
        --max-num-seqs|--max-num-seqs=*|--max-running-requests|--max-running-requests=*)
            die "server 併發上限由自適應探針守衛，不能由 extra server args 覆蓋" ;;
        --reasoning-parser|--reasoning-parser=*)
            [[ "$ENGINE" == sglang ]] && die "SGLang 的 --reasoning-parser qwen3 是強制品質守衛，不能由 CLOUD_LLM_EXTRA_SERVER_ARGS 覆蓋" ;;
    esac
done
if [[ ${#_extra[@]} -gt 0 ]]; then
    SERVER_ARGS+=("${_extra[@]}")
fi

# ---------- 開機 ----------
if [[ "$PROVIDER" == vast ]]; then
    ENV_STR="-p ${PORT}:${PORT}"
    [[ -n "${HF_TOKEN:-}" ]] && ENV_STR="$ENV_STR -e HF_TOKEN=${HF_TOKEN}"
    ROWS="$(vast_lib_pick_offers "$GPU" "$DISK_GB" "$MAX_DPH")" \
        || die "Vast.ai 沒有符合條件的報價（${GPU}、≤${MAX_DPH} USD/h）。放寬 --max-dph 或換 --gpu"
    TRIES=0
    while IFS=$'\t' read -r OFFER_ID OFFER_DESC; do
        [[ -n "$OFFER_ID" ]] || continue
        TRIES=$((TRIES + 1)); (( TRIES <= 3 )) || break
        log "試報價 ${OFFER_ID}（$TRIES/3）：$OFFER_DESC"
        if OUT="$(vast_lib_create_instance_args "$OFFER_ID" "$IMAGE" "$DISK_GB" "$LABEL" "$ENV_STR" "${SERVER_ARGS[@]}")"; then
            INSTANCE_ID="$OUT"; break
        fi
        # 解析失敗≠沒開機（09-04 首跑就這樣連漏三台）。換下一張前先依 label 回查，有就認領。
        if ADOPTED="$(vast_lib_find_live_instance_by_label "$LABEL")" && [[ -n "$ADOPTED" ]]; then
            log "報價 ${OFFER_ID} 回應看不懂但清單裡已有本次 label 的機器 ${ADOPTED}，認領它"
            INSTANCE_ID="$ADOPTED"; break
        fi
        log "報價 ${OFFER_ID} 開不起來（${OUT}），換下一張"
    done <<<"$ROWS"
    [[ -n "$INSTANCE_ID" ]] || die "Vast.ai 開機失敗（試了 $TRIES 張報價）"
else
    BODY="$(mktemp "${TMPDIR:-/tmp}/cloud_llm.XXXXXX")"
    ARGS_STR="$(printf '%q ' "${SERVER_ARGS[@]}")"
    jq -n --arg name "$LABEL" --arg image "$IMAGE" --arg gpu "$GPU" --arg args "$ARGS_STR" \
          --argjson disk "$DISK_GB" --arg hf "${HF_TOKEN:-}" '
        {name:$name, cloud:"SECURE", gpu:{id:$gpu, count:1, minCudaVersion:"12.8"},
         image:$image, disk:$disk, ports:["\(8000)/tcp"], args:$args, startSsh:false,
         env: (if $hf != "" then {HF_TOKEN:$hf} else {} end)}' >"$BODY"
    RESP="$(runpod_lib_request nonfatal POST "/pods" "$BODY")" || true
    rm -f "$BODY"
    INSTANCE_ID="$(jq -r '.id // .pod.id // empty' <<<"${RESP:-}" 2>/dev/null || true)"
    if [[ -z "$INSTANCE_ID" ]]; then
        INSTANCE_ID="$(runpod_lib_find_live_pod_by_name "$LABEL")"
        [[ -n "$INSTANCE_ID" ]] && { log "⚠️  回應沒 id 但清單裡有同名 pod ${INSTANCE_ID}，砍掉"; }
        die "RunPod 開機失敗：${RESP:-<空>}"
    fi
fi
printf '%s\n' "$INSTANCE_ID" >"$RUN_DIR/instance.id"
log "已開機 ${INSTANCE_ID}（開始計費）"

# ---------- 等伺服器就緒（拉映像 → 下載模型 → 載入） ----------
HOST=""; HPORT=""
boot_deadline=$(( $(date +%s) + BOOT_WAIT_MIN * 60 ))
last_msg=""; stall_since=$(date +%s)
while :; do
    now=$(date +%s)
    (( now < boot_deadline )) || die "等了 ${BOOT_WAIT_MIN} 分鐘伺服器還沒就緒，已砍機（映像＋模型載入太慢，換一張報價或調 CLOUD_LLM_BOOT_WAIT_MIN）"
    if [[ "$PROVIDER" == vast ]]; then
        record="$(vast_lib_instance_record "$INSTANCE_ID" 2>/dev/null || echo "")"
        # 空紀錄用 null 當預設：${var:-{}} 會被 } 截斷成壞 JSON（09-04 實測 status 印成「?」加換行）
        status="$(jq -r '(.actual_status // "null") | ascii_downcase' <<<"${record:-null}" 2>/dev/null || echo "?")"
        vast_lib_status_is_dead "$status" && die "機器狀態 ${status}，不會再變成可用，已砍機"
        msg="$(jq -r '.status_msg // ""' <<<"${record:-null}" 2>/dev/null || echo "")"
        if [[ "$msg" != "$last_msg" ]]; then last_msg="$msg"; stall_since=$now
        elif [[ "$status" == loading && $(( now - stall_since )) -ge $(( BOOT_STALL_MIN * 60 )) ]]; then
            die "拉映像進度 ${BOOT_STALL_MIN} 分鐘沒變，這台主機拉不到映像，已砍機"
        fi
        if [[ -n "$record" ]] && EP="$(vast_lib_port_endpoint_from_record "$record" "$PORT")"; then
            HOST="${EP%%$'\t'*}"; HPORT="${EP##*$'\t'}"
        fi
    else
        record="$(runpod_lib_pod_record "$INSTANCE_ID" 2>/dev/null || echo "")"
        status="$(jq -r '.status // "?"' <<<"${record:-null}" 2>/dev/null || echo "?")"
        if [[ -n "$record" ]] && EP="$(runpod_lib_port_endpoint_from_record "$record" "$PORT")"; then
            HOST="${EP%%$'\t'*}"; HPORT="${EP##*$'\t'}"
        fi
    fi
    if [[ -n "$HOST" && -n "$HPORT" ]]; then
        if curl -s -m 8 -H "Authorization: Bearer $API_KEY" "http://$HOST:$HPORT/v1/models" 2>/dev/null | jq -e --arg m "$MODEL" '.data[]? | select(.id == $m)' >/dev/null 2>&1; then
            break
        fi
        log "狀態 ${status}，端點 $HOST:$HPORT 已有、${ENGINE} 還在載入模型…"
        # 每五輪印一次容器紀錄尾巴：server 參數錯會反覆重啟，不印的話只看得到「還在載入」直到預算用完
        POLLS=$(( ${POLLS:-0} + 1 ))
        if [[ "$PROVIDER" == vast && $(( POLLS % 5 )) -eq 0 ]]; then
            vast_lib_cli logs "$INSTANCE_ID" --tail 5 2>/dev/null | grep -v '^$' | tail -3 | sed 's/^/[cloud-llm]   容器: /' >&2 || true
        fi
    else
        log "狀態 ${status}，等對外埠…"
    fi
    sleep "$POLL_SECONDS"
done
ENDPOINT="http://$HOST:$HPORT"
log "${ENGINE} 就緒：${ENDPOINT}（模型 ${MODEL}），等了 $(( ( $(date +%s) - (boot_deadline - BOOT_WAIT_MIN * 60) ) / 60 )) 分鐘"

# Thinking preflight 必須是啟動後的第一個 completion request，兩個引擎都要跑。
# Qwopus 的微調模板可能讓 enable_thinking=false 靜默失效；content 出現 think tag 或
# reasoning_content 非空都立即砍機，不把受污染輸出帶進翻譯／benchmark。
    PROBE_PAYLOAD="$(jq -cn --arg model "$MODEL" '{model:$model,messages:[{role:"user",content:"Translate into Taiwan Traditional Chinese. Output only the translation: Connection test."}],max_tokens:64,temperature:0,chat_template_kwargs:{enable_thinking:false}}')"
    if ! PROBE_RESPONSE="$(curl -fsS -m 180 -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
        --data-binary "$PROBE_PAYLOAD" "$ENDPOINT/v1/chat/completions")"; then
        die "${ENGINE} thinking preflight 請求失敗，未開始翻譯"
    fi
    jq -e '.choices[0].message.content | type == "string" and length > 0' \
        <<<"$PROBE_RESPONSE" >/dev/null 2>&1 \
        || die "${ENGINE} thinking preflight content 必須是非空字串，未開始翻譯"
    jq -e '.choices[0].message as $m | ((($m | has("reasoning_content")) | not) or $m.reasoning_content == null or $m.reasoning_content == "")' \
        <<<"$PROBE_RESPONSE" >/dev/null 2>&1 \
        || die "${ENGINE} thinking preflight reasoning_content 非空，未開始翻譯"
    PROBE_CONTENT="$(jq -r '.choices[0].message.content' <<<"$PROBE_RESPONSE")"
    # grep 是逐行工具；先拿掉換行，才能抓到 `<think\n>` 這類跨行 tag。
    PROBE_CONTENT_ONE_LINE="$(printf '%s' "$PROBE_CONTENT" | tr -d '\r\n')"
    if grep -Eiq '<[[:space:]]*/?[[:space:]]*think([[:space:]>])' <<<"$PROBE_CONTENT_ONE_LINE"; then
        die "${ENGINE} thinking preflight content 含 think 標籤，未開始翻譯"
    fi
log "${ENGINE} thinking preflight 通過：content 無 think 標籤、reasoning_content 為空"

# ---------- 自適應併發探針 ----------
# 每波使用不同、真實大小的大塊；小段會高估單筆速度而選到危險的 N。第一波 prefix
# cache 是冷的，量測偏保守，方向安全。探針也會自然反映 cache preempt 的延遲成本。
PROBE_RESULT="$RUN_DIR/adaptive-concurrency.json"
if [[ -n "${CLOUD_LLM_TEST_PROBE_CMD:-}" ]]; then
    read -r -a ADAPTIVE_PROBE_CMD <<<"$CLOUD_LLM_TEST_PROBE_CMD"
else
    ADAPTIVE_PROBE_CMD=(python3 "$SCRIPT_DIR/adaptive_concurrency_probe.py")
fi
set +e
OMLX_API_KEY="$API_KEY" "${ADAPTIVE_PROBE_CMD[@]}" --endpoint "$ENDPOINT" --model "$MODEL" \
    --load "$PROBE_LOAD" --out "$PROBE_RESULT" --gpu "$GPU" --profile "$PROFILE"
PROBE_RC=$?
set -e
if [[ $PROBE_RC -eq 0 ]] && SAFE_CONCURRENCY="$(jq -er '.selected_concurrency | select(type == "number")' "$PROBE_RESULT" 2>/dev/null)"; then
    PROBE_SUMMARY="$(jq -r '[.waves[] | "N=\(.n): \(.mean_single_tok_s) tok/s, max \(.max_latency_s)s"] | join("; ")' "$PROBE_RESULT")"
    log "自適應探針：${PROBE_SUMMARY}；安全 N=${SAFE_CONCURRENCY}"
    if [[ "$(jq -r '.passed' "$PROBE_RESULT")" != true ]]; then
        log "⚠️  N=24/16/12/8 都未達 20% 跑飛餘裕，退到 8 繼續翻譯；請考慮換卡"
    fi
    if [[ "$CONCURRENCY_EXPLICIT" == true ]]; then
        if (( CONCURRENCY > SAFE_CONCURRENCY )); then
            log "⚠️  明傳併發 $CONCURRENCY 高於探針安全值 ${SAFE_CONCURRENCY}；依使用者指定繼續"
        else
            log "明傳併發 ${CONCURRENCY}（探針安全值 ${SAFE_CONCURRENCY}），依使用者指定"
        fi
    else
        CONCURRENCY="$SAFE_CONCURRENCY"
    fi
else
    log "⚠️  自適應併發探針失敗，無法取得可靠量測；併發退回全域預設 16，繼續翻譯"
    if [[ "$CONCURRENCY_EXPLICIT" == false ]]; then
        CONCURRENCY=16
    else
        log "明傳 CLOUD_LLM_CONCURRENCY=$CONCURRENCY 仍優先於 fallback"
    fi
fi

printf 'OMLX_HOST=%s\nOMLX_MODEL=%s\nOMLX_API_KEY=%s\n' "$ENDPOINT" "$MODEL" "$API_KEY" >"$RUN_DIR/endpoint.env"
chmod 600 "$RUN_DIR/endpoint.env"

# ---------- 翻譯（看門狗：超時送 TERM，trap 接手砍機） ----------
if [[ -n "${CLOUD_LLM_TEST_TRANSLATE_CMD:-}" ]]; then
    read -r -a TRANSLATE_CMD <<<"$CLOUD_LLM_TEST_TRANSLATE_CMD"
else
    TRANSLATE_CMD=(python3 "$REPO_ROOT/scripts/translate_book_ollama.py")
fi
CONCURRENCY_ARGS=()
if [[ "$CONCURRENT_CHAPTERS" == 1 ]]; then
    CONCURRENCY_ARGS=(--concurrent-chapters --max-concurrent-requests "$CONCURRENCY")
fi
# Bash 3.2 + set -u 不能直接展開空陣列；${A[@]+"${A[@]}"} 在空陣列時給 0 個參數，
# 非空時仍保留每個參數的邊界。macOS 沒 Homebrew bash 或 launchd PATH 常會走 /bin/bash 3.2。
log "開始翻譯：${TRANSLATE_CMD[*]} --engine omlx --omlx-host $ENDPOINT --omlx-model $MODEL ${CONCURRENCY_ARGS[@]+"${CONCURRENCY_ARGS[@]}"} ${TRANSLATE_ARGS[*]}"
set +e
OMLX_API_KEY="$API_KEY" "${TRANSLATE_CMD[@]}" --engine omlx --omlx-host "$ENDPOINT" --omlx-model "$MODEL" ${CONCURRENCY_ARGS[@]+"${CONCURRENCY_ARGS[@]}"} "${TRANSLATE_ARGS[@]}" &
TPID=$!
# 看門狗的輸出一定要導掉：它那個 sleep 若成了孤兒還握著 stdout/stderr，呼叫端（含測試的 subprocess）
# 會等不到 EOF 一直掛著。收工時連 sleep 一起殺（pkill -P）。
( sleep $(( MAX_HOURS * 3600 )); kill -TERM "$TPID" 2>/dev/null ) >/dev/null 2>&1 </dev/null &
WATCHDOG=$!
wait "$TPID"; TRC=$?
pkill -P "$WATCHDOG" 2>/dev/null || true; kill "$WATCHDOG" 2>/dev/null || true; wait "$WATCHDOG" 2>/dev/null || true
set -e
if [[ $TRC -ne 0 ]]; then
    log "翻譯器離開碼 ${TRC}（超過 ${MAX_HOURS} 小時會是 143）"
fi

# ---------- 收尾 ----------
if [[ "$KEEP" == true ]]; then
    KEEP_ACTIVE=true
    log "--keep：端點留著，$RUN_DIR/endpoint.env 可給下一本用；砍：$0 --stop $RUN_DIR"
    trap - EXIT INT TERM HUP
    exit "$TRC"
fi
terminate_instance || { log "⚠️  翻譯結束但砍機失敗，機器仍在計費"; exit 1; }
trap - EXIT INT TERM HUP
[[ $TRC -eq 0 ]] && log "完成，機器已砍"
exit "$TRC"
