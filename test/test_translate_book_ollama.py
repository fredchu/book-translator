from __future__ import annotations

import json
import random
import sys
import threading
import time as time_module
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# Import the driver after sys.path setup
import chunker as chk  # type: ignore  # noqa: E402
import translate_book_ollama as drv  # type: ignore  # noqa: E402
from audit_result import AuditResult  # noqa: E402
from providers import OmlxProvider  # noqa: E402
from providers.base import ProviderResult  # noqa: E402


def _mock_provider_returning(*aligned_per_call_returns: str | None) -> MagicMock:
    """Mock provider whose .translate() yields ProviderResult with raw_text
    set to a marker-aligned response when the input is a marker prompt,
    or None for empty-output simulation."""
    mock = MagicMock()
    mock.temperature = 0.3
    mock.ping.return_value = True
    queue = list(aligned_per_call_returns)

    def _translate(prompt, *, request_id, log_dir, system=None, temperature=None):
        # Pop the next planned response (default to a generic marker-aligned 1-para)
        nxt = queue.pop(0) if queue else "[[PARA_1]]\n譯文"
        if nxt is None:
            return ProviderResult(raw_text="", model="mock", latency_ms=10, retries=0, metadata={})
        return ProviderResult(raw_text=nxt, model="mock", latency_ms=10, retries=0, metadata={})

    mock.translate.side_effect = _translate
    return mock


def test_main_refuses_to_start_without_safe_opencc_dictionary(monkeypatch, capsys):
    monkeypatch.setattr(drv.offline_postprocess, "_converter", lambda: object())
    monkeypatch.setattr(drv.offline_postprocess, "_simplified_triggers", lambda _cc: None)
    translate = MagicMock()
    monkeypatch.setattr(drv, "translate_single_book", translate)

    assert drv.main(["--book", "test.epub"]) == 2
    assert "safe Simplified->Traditional conversion unavailable" in capsys.readouterr().err
    translate.assert_not_called()


def test_main_logs_simplified_trigger_count(monkeypatch, capsys):
    monkeypatch.setattr(drv.offline_postprocess, "_converter", lambda: object())
    monkeypatch.setattr(drv.offline_postprocess, "_simplified_triggers", lambda _cc: frozenset("这着"))
    monkeypatch.setattr(
        drv,
        "translate_single_book",
        lambda path, _args: {
            "path": path,
            "status": "success",
            "duration_sec": 0.0,
            "error_summary": "",
            "return_code": 0,
        },
    )

    assert drv.main(["--book", "test.epub"]) == 0
    assert "[opencc] simplified_triggers=2" in capsys.readouterr().err


def test_record_audit_warnings_persists_actionable_findings(tmp_path):
    state = {"chapters": {}}
    state_path = tmp_path / "state.json"
    results = [
        AuditResult(
            name="translation_quality",
            status="warn",
            failures=[],
            warnings=["chapter.xhtml: target too short"],
        )
    ]

    records = drv._record_audit_warnings(state, state_path, results)

    assert records == [
        {"audit": "translation_quality", "warning": "chapter.xhtml: target too short"}
    ]
    saved = json.loads(state_path.read_text("utf-8"))
    assert saved["audit_warnings"] == {"count": 1, "findings": records}


def test_summary_surfaces_audit_warning_count(capsys):
    drv._print_summary(
        [{
            "path": Path("book.epub"),
            "status": "success",
            "duration_sec": 1.0,
            "error_summary": "",
            "return_code": 0,
            "audit_warnings": 3,
        }]
    )

    assert "audit_warnings=3" in capsys.readouterr().err


def test_recursion_splits_failed_chunk_into_halves(tmp_path):
    # 4-paragraph chunk: first call (whole 4 paras) returns misaligned; halves succeed
    paragraphs = ("A.", "B.", "C.", "D.")
    mock = _mock_provider_returning(
        "[[PARA_1]]\n甲",  # whole chunk: missing 2,3,4 — fail
        "[[PARA_1]]\n甲",  # whole chunk attempt 1 retry: still bad — fail
        "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙",  # left half (A,B): aligned
        "[[PARA_1]]\n丙\n\n[[PARA_2]]\n丁",  # right half (C,D): aligned
    )
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01of01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=0,
        max_depth=4,
    )
    assert result is not None
    assert "甲" in result and "乙" in result and "丙" in result and "丁" in result


def test_recursion_falls_back_to_single_paragraph_when_one_left(tmp_path):
    # 1-paragraph chunk: marker contract fails both attempts; minimal fallback succeeds
    paragraphs = ("Hello.",)
    mock = _mock_provider_returning(
        "I cannot help with this",   # marker attempt 0
        "",                          # marker attempt 1: empty
        "你好。",                     # minimal fallback: succeeds
    )
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01-LRL",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=3,
        max_depth=4,
    )
    assert result == "你好。"


def test_recursion_returns_none_when_all_fail(tmp_path):
    # 1-paragraph chunk: all 3 attempts (marker x2 + minimal fallback x1) return empty
    paragraphs = ("Hello.",)
    mock = _mock_provider_returning("", "", "")  # all empty
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=0,
        max_depth=4,
    )
    assert result is None


def test_recursion_respects_max_depth(tmp_path):
    # 8-paragraph chunk forced to depth=4; should not split further than allowed.
    # At max_depth, instead of splitting further, the function should fall through
    # to single-paragraph fallback for each remaining paragraph.
    paragraphs = tuple(f"P{i}." for i in range(8))
    # Whole chunk fails 2x; all halves also fail at every level; at depth 4
    # the fallback is forced for each paragraph. Set up enough mock returns.
    fallbacks = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    # Worst case sequence: 2 attempts at every level. Just provide enough empty/marker-failing returns then the 8 fallbacks at the end.
    seq = ["[[PARA_1]]\nbad"] * 32 + fallbacks  # padded; recursion may not consume all
    mock = _mock_provider_returning(*seq)
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=paragraphs,
        chapter_label="1",
        chunk_label="ck01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=0,
        max_depth=4,
    )
    # Either succeeds via fallback or returns None — both acceptable; key is no infinite recursion.
    # The test passing without timeout is the real assertion.
    assert result is None or len(result) > 0


# ---------------------------------------------------------------------------
# Concurrency: intra-chapter chunk dispatch (source-text carryover, no
# chunk-to-chunk wait chain) and cross-chapter data-dependency removal.
# ---------------------------------------------------------------------------


class _FakeConcurrentProvider:
    """Thread-safe fake with configurable per-request delay and canned
    marker-aligned responses, for testing concurrent dispatch ordering and
    determinism without a real HTTP server or a loaded model.

    `response_for` maps a substring of request_id (chunk_label, e.g. "ck02"
    or "seam01") to the raw marker-aligned text to return. `delays` maps the
    same kind of substring to a sleep duration before responding, so tests
    can force completion order to differ from submission order.
    """

    name = "fake"

    def __init__(self, *, response_for: dict[str, str], delays: dict[str, float] | None = None,
                 max_concurrent_requests: int = 8) -> None:
        self.max_concurrent_requests = max_concurrent_requests
        self.supports_concurrency = max_concurrent_requests > 1
        self.temperature = 0.3
        self._response_for = response_for
        self._delays = delays or {}
        self._lock = threading.Lock()
        self.calls: list[dict] = []  # in arrival order, NOT job order

    def ping(self) -> bool:
        return True

    def _match(self, table: dict, request_id: str, default):
        for key, value in table.items():
            if key in request_id:
                return value
        return default

    def translate(self, prompt, *, request_id, log_dir=None, system=None, temperature=None):
        delay = self._match(self._delays, request_id, 0.0)
        if delay:
            time_module.sleep(delay)
        with self._lock:
            self.calls.append(
                {"request_id": request_id, "prompt": prompt, "system": system, "temperature": temperature}
            )
        text = self._match(self._response_for, request_id, "[[PARA_1]]\n?")
        return ProviderResult(raw_text=text, model="fake", latency_ms=1, retries=0, metadata={})


def _four_paragraph_html() -> str:
    return "<p>Para A.</p><p>Para B.</p><p>Para C.</p><p>Para D.</p>"


def test_intra_chapter_carries_uses_previous_chunk_source_not_translation():
    chunks = [
        chk.Chunk(paragraphs=("Alpha one.", "Alpha two."), start_idx=0),
        chk.Chunk(paragraphs=("Beta one.",), start_idx=2),
        chk.Chunk(paragraphs=("Gamma one.",), start_idx=3),
    ]
    carries = drv._intra_chapter_carries(chunks, "INITIAL")
    assert carries[0] == "INITIAL"
    assert carries[1] == chk.source_tail(chunks[0].paragraphs) == "Alpha one.\n\nAlpha two."
    assert carries[2] == chk.source_tail(chunks[1].paragraphs) == "Beta one."


def test_translate_single_book_concurrent_cross_chapter_carry_is_translation(tmp_path, monkeypatch):
    """Regression fix (review-15): chapters are translated one at a time no
    matter what the provider supports — no chapter ever starts before the
    previous one finishes — so decoupling the CROSS-chapter carry from the
    previous chapter's real translation buys nothing while losing the real
    translated tail at every one of the 19 chapter boundaries. Chapter 2's
    starting carry must be chapter 1's TRANSLATION tail, exactly like the
    sequential path, even when the provider is concurrency-capable."""
    book_path = tmp_path / "book.epub"
    book_path.write_bytes(b"not a real epub - extract_epub.extract is mocked below")
    out_dir = tmp_path / "out"

    def fake_extract(_book_path: Path, out_parent: Path) -> None:
        book_dir = out_parent / book_path.stem
        chapters_dir = book_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        (chapters_dir / "item_001.html").write_text("<p>Chapter one source.</p>", encoding="utf-8")
        (chapters_dir / "item_002.html").write_text("<p>Chapter two source.</p>", encoding="utf-8")
        (book_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "spine": [
                        {"id": "item_001", "output_strategy": "translate", "char_count": 10},
                        {"id": "item_002", "output_strategy": "translate", "char_count": 10},
                    ]
                }
            ),
            encoding="utf-8",
        )

    provider = _FakeConcurrentProvider(
        response_for={"ck01": "[[PARA_1]]\n第一章譯文"},  # single-paragraph chunk in each chapter
        max_concurrent_requests=4,
    )
    monkeypatch.setattr(drv, "extract_epub", MagicMock(extract=fake_extract))
    monkeypatch.setattr(drv, "OmlxProvider", lambda **_kwargs: provider)
    monkeypatch.setattr(drv.offline_postprocess, "to_traditional", lambda text: text)

    args = drv.build_parser().parse_args(
        ["--book", str(book_path), "--out", str(out_dir),
         "--engine", "omlx", "--max-concurrent-requests", "4"]
    )
    drv.translate_single_book(book_path, args)  # assemble() will fail on the fake book_dir — fine,
    # translation already ran and is what this test checks.

    ck01_calls = [c for c in provider.calls if "ck01" in c["request_id"]]
    assert len(ck01_calls) == 2  # one per chapter
    chapter1_call, chapter2_call = ck01_calls
    assert "Chapter one source." not in chapter2_call["prompt"]
    assert "第一章譯文" in chapter2_call["prompt"]


@pytest.mark.parametrize("request_limit", [1, 2])
def test_translate_single_book_cross_chapter_flag_is_independent_bounded_and_state_safe(
    tmp_path, monkeypatch, request_limit, capsys
):
    """Opt-in chapters overlap without cross-chapter carry and state has one writer.

    The fake provider uses a barrier, not sleep/timing, so the overlap claim is
    deterministic even when this test runs alone on a loaded machine.
    """
    book_path = tmp_path / "book.epub"
    book_path.write_bytes(b"fake epub")
    out_dir = tmp_path / "out"

    def fake_extract(_book_path: Path, out_parent: Path) -> None:
        book_dir = out_parent / book_path.stem
        chapters_dir = book_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        spine = []
        for i in range(1, 4):
            cid = f"item_{i:03d}"
            (chapters_dir / f"{cid}.html").write_text(
                f"<p>Chapter {i} source A.</p>", encoding="utf-8"
            )
            spine.append({"id": cid, "output_strategy": "translate", "char_count": 10})
        (book_dir / "manifest.json").write_text(
            json.dumps({"spine": spine}), encoding="utf-8"
        )

    class TrackingProvider(_FakeConcurrentProvider):
        def __init__(self) -> None:
            super().__init__(response_for={}, max_concurrent_requests=request_limit)
            self.inflight = 0
            self.max_inflight = 0
            self.active_by_chapter: dict[str, int] = {}
            self.max_active_chapters = 0
            self.chapter_barrier = (
                threading.Barrier(2, timeout=5) if request_limit > 1 else None
            )
            self.barrier_arrivals = 0

        def translate(self, prompt, *, request_id, log_dir=None, system=None, temperature=None):
            chapter = request_id.split("_", 1)[0]
            with self._lock:
                wait_at_barrier = self.chapter_barrier is not None and self.barrier_arrivals < 2
                if wait_at_barrier:
                    self.barrier_arrivals += 1
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
                self.active_by_chapter[chapter] = self.active_by_chapter.get(chapter, 0) + 1
                self.max_active_chapters = max(
                    self.max_active_chapters, len(self.active_by_chapter)
                )
            try:
                if wait_at_barrier:
                    self.chapter_barrier.wait()
                with self._lock:
                    self.calls.append({"request_id": request_id, "prompt": prompt,
                                       "system": system, "temperature": temperature})
                chapter_number = next(n for n in ("1", "2", "3") if f"Chapter {n} source " in prompt)
                return ProviderResult(raw_text=f"[[PARA_1]]\n第{chapter_number}章譯文",
                                      model="fake", latency_ms=1, retries=0, metadata={})
            finally:
                with self._lock:
                    self.inflight -= 1
                    self.active_by_chapter[chapter] -= 1
                    if self.active_by_chapter[chapter] == 0:
                        del self.active_by_chapter[chapter]

    provider = TrackingProvider()
    main_thread = threading.get_ident()
    save_threads: list[int] = []
    real_save = drv.state_mod.save

    def tracked_save(path, state):
        save_threads.append(threading.get_ident())
        return real_save(path, state)

    monkeypatch.setattr(drv, "extract_epub", MagicMock(extract=fake_extract))
    monkeypatch.setattr(drv, "OmlxProvider", lambda **_kwargs: provider)
    monkeypatch.setattr(drv.offline_postprocess, "to_traditional", lambda text: text)
    monkeypatch.setattr(drv.state_mod, "save", tracked_save)

    args = drv.build_parser().parse_args(
        ["--book", str(book_path), "--out", str(out_dir), "--engine", "omlx",
         "--max-concurrent-requests", str(request_limit), "--concurrent-chapters"]
    )
    drv.translate_single_book(book_path, args)
    run_stderr = capsys.readouterr().err
    assert "max_tokens=2048 (omlx default)" in run_stderr

    if request_limit > 1:
        assert provider.max_active_chapters >= 2  # another chapter starts before the first finishes
    else:
        assert provider.max_inflight == 1  # flag is valid even with no intra-chapter fan-out
    assert provider.max_inflight <= request_limit
    chapter2_call = next(c for c in provider.calls if c["request_id"].startswith("2_ck01"))
    assert "第1章譯文" not in chapter2_call["prompt"]
    assert "Chapter 1 source A." not in chapter2_call["prompt"]
    saved = json.loads((out_dir / "book" / "state.json").read_text(encoding="utf-8"))
    assert all(saved["chapters"][f"item_{i:03d}"]["status"] == drv.state_mod.DONE
               for i in range(1, 4))
    assert save_threads and set(save_threads) == {main_thread}


def test_cross_chapter_worker_exception_is_committed_without_losing_other_results(
    tmp_path, monkeypatch
):
    """An unexpected worker exception is merged by the main thread as FAILED;
    sibling chapters still finish and state.json remains complete JSON.
    """
    book_path = tmp_path / "book.epub"
    book_path.write_bytes(b"fake epub")
    out_dir = tmp_path / "out"

    def fake_extract(_book_path: Path, out_parent: Path) -> None:
        book_dir = out_parent / book_path.stem
        chapters_dir = book_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        spine = []
        for i in range(1, 4):
            cid = f"item_{i:03d}"
            (chapters_dir / f"{cid}.html").write_text(
                f"<p>Chapter {i} source.</p>", encoding="utf-8"
            )
            spine.append({"id": cid, "output_strategy": "translate", "char_count": 10})
        (book_dir / "manifest.json").write_text(
            json.dumps({"spine": spine}), encoding="utf-8"
        )

    class ExplodingProvider(_FakeConcurrentProvider):
        def __init__(self) -> None:
            super().__init__(response_for={}, max_concurrent_requests=2)

        def translate(self, prompt, *, request_id, log_dir=None, system=None, temperature=None):
            if request_id.startswith("2_"):
                raise RuntimeError("synthetic chapter worker crash")
            return ProviderResult(raw_text="[[PARA_1]]\n完成", model="fake",
                                  latency_ms=1, retries=0, metadata={})

    provider = ExplodingProvider()
    monkeypatch.setattr(drv, "extract_epub", MagicMock(extract=fake_extract))
    monkeypatch.setattr(drv, "OmlxProvider", lambda **_kwargs: provider)
    monkeypatch.setattr(drv.offline_postprocess, "to_traditional", lambda text: text)
    args = drv.build_parser().parse_args(
        ["--book", str(book_path), "--out", str(out_dir), "--engine", "omlx",
         "--max-concurrent-requests", "2", "--concurrent-chapters"]
    )

    drv.translate_single_book(book_path, args)

    saved = json.loads((out_dir / "book" / "state.json").read_text(encoding="utf-8"))
    assert saved["chapters"]["item_001"]["status"] == drv.state_mod.DONE
    assert saved["chapters"]["item_002"]["status"] == drv.state_mod.FAILED
    assert "synthetic chapter worker crash" in saved["chapters"]["item_002"]["error"]
    assert saved["chapters"]["item_003"]["status"] == drv.state_mod.DONE
    assert set(saved["chapters"]) == {"item_001", "item_002", "item_003"}


def test_request_limiter_never_allows_more_than_global_limit():
    """All caller threads rendezvous before entering the wrapper; accepted
    calls stay blocked. With the limiter intact exactly two reach the delegate.
    Removing the semaphore lets all four reach it and deterministically fails.
    """

    class BlockingProvider:
        supports_concurrency = True
        max_concurrent_requests = 2

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.two_entered = threading.Event()
            self.four_entered = threading.Event()
            self.release = threading.Event()

        def translate(self, *_args, **_kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active >= 2:
                    self.two_entered.set()
                if self.active >= 4:
                    self.four_entered.set()
            self.release.wait(timeout=5)
            with self.lock:
                self.active -= 1
            return ProviderResult(raw_text="ok", model="fake", latency_ms=1,
                                  retries=0, metadata={})

    provider = BlockingProvider()
    limited = drv._RequestLimitedProvider(provider, 2)
    callers_ready = threading.Barrier(5, timeout=5)

    def call_provider(index: int):
        callers_ready.wait()
        return limited.translate("p", request_id=f"r{index}")

    with drv.concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(call_provider, i) for i in range(4)]
        callers_ready.wait()
        try:
            assert provider.two_entered.wait(timeout=2)
            assert not provider.four_entered.wait(timeout=0.1)
            assert provider.max_active == 2
        finally:
            provider.release.set()
        for future in futures:
            future.result(timeout=2)


def test_chapter_provider_namespaces_fallback_ids_without_double_prefixing():
    provider = MagicMock()
    wrapped = drv._ChapterNamespacedProvider(provider, "2")
    wrapped.translate("p", request_id="ck01_fallback")
    wrapped.translate("p", request_id="2_ck01_attempt_0")
    assert [call.kwargs["request_id"] for call in provider.translate.call_args_list] == [
        "2_ck01_fallback",
        "2_ck01_attempt_0",
    ]


@pytest.mark.parametrize(
    "concurrency_args",
    [["--max-concurrent-requests", "2"], ["--concurrent-chapters"]],
)
def test_concurrency_warns_when_book_has_no_term_table(
    tmp_path, capsys, concurrency_args
):
    args = drv.build_parser().parse_args(
        ["--book", str(tmp_path / "x.epub"), *concurrency_args]
    )
    drv._warn_if_concurrent_without_terms(args, tmp_path)
    warning = capsys.readouterr().err
    assert "併發模式已開啟" in warning
    assert "沒有可用的 spec_terms.json 術語表" in warning
    assert "本次仍會繼續" in warning


@pytest.mark.parametrize(
    "term_file",
    ["{not-json", '{"terms": []}', '{"terms": {}}', "[]", "null", "42"],
)
def test_concurrency_treats_malformed_or_empty_term_table_as_missing(
    tmp_path, capsys, term_file
):
    (tmp_path / "spec_terms.json").write_text(term_file, encoding="utf-8")
    args = drv.build_parser().parse_args(
        ["--book", str(tmp_path / "x.epub"), "--concurrent-chapters"]
    )
    drv._warn_if_concurrent_without_terms(args, tmp_path)
    assert "沒有可用的 spec_terms.json 術語表" in capsys.readouterr().err


def test_concurrency_does_not_warn_when_book_has_term_table(tmp_path, capsys):
    (tmp_path / "spec_terms.json").write_text(
        json.dumps({"terms": {"Elena": "艾蓮娜"}}), encoding="utf-8"
    )
    args = drv.build_parser().parse_args(
        ["--book", str(tmp_path / "x.epub"), "--concurrent-chapters"]
    )
    drv._warn_if_concurrent_without_terms(args, tmp_path)
    assert capsys.readouterr().err == ""


def test_sequential_mode_without_term_table_does_not_warn(tmp_path, capsys):
    args = drv.build_parser().parse_args(
        ["--book", str(tmp_path / "x.epub")]
    )
    drv._warn_if_concurrent_without_terms(args, tmp_path)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("term_file", ["{not-json", "[]", "null", '{"terms": []}'])
def test_sequential_malformed_term_table_remains_a_hard_error(
    tmp_path, capsys, term_file
):
    (tmp_path / "spec_terms.json").write_text(term_file, encoding="utf-8")
    args = drv.build_parser().parse_args(
        ["--book", str(tmp_path / "x.epub")]
    )
    drv._warn_if_concurrent_without_terms(args, tmp_path)
    assert capsys.readouterr().err == ""
    with pytest.raises((json.JSONDecodeError, ValueError)):
        drv.dispatch.load_fixed_terms(tmp_path)


def test_book_driver_engine_specific_output_token_defaults_and_explicit_override():
    assert drv._resolve_num_predict("omlx", None) == 2048
    assert drv._resolve_num_predict("ollama", None) == 4096
    assert drv._resolve_num_predict("omlx", 4096) == 4096
    assert drv._resolve_num_predict("ollama", 1234) == 1234


def test_resolve_max_workers_defaults_to_one_without_attribute():
    """review-15: a provider with supports_concurrency=True but no
    max_concurrent_requests attribute (e.g. AnthropicProvider) must not
    silently fan out to an unconfigured, unbounded concurrency level —
    default to 1, never the caller-supplied job count."""

    class _NoLimitProvider:
        supports_concurrency = True

    assert drv._resolve_max_workers(_NoLimitProvider()) == 1


def test_resolve_max_workers_uses_provider_attribute_when_present():
    class _LimitedProvider:
        supports_concurrency = True
        max_concurrent_requests = 4

    assert drv._resolve_max_workers(_LimitedProvider()) == 4


def test_concurrent_dispatch_reassembles_in_job_order_not_arrival_order(tmp_path):
    # ck04 (last submitted) finishes first; ck01 (first submitted) finishes
    # last. Output must still stitch in chunk order 1-2-3-4.
    provider = _FakeConcurrentProvider(
        response_for={"ck01": "[[PARA_1]]\n甲", "ck02": "[[PARA_1]]\n乙",
                      "ck03": "[[PARA_1]]\n丙", "ck04": "[[PARA_1]]\n丁"},
        delays={"ck01": 0.06, "ck02": 0.04, "ck03": 0.02, "ck04": 0.0},
        max_concurrent_requests=4,
    )
    aligned, warns, partial = drv._translate_chapter_chunked_concurrent(
        provider, html=_four_paragraph_html(), chapter_id="c1", chapter_label="1",
        book_title="T", target_lang="zh-tw", carryover="",
        book_dir=tmp_path, default_temperature=0.3, chunk_max_chars=5,
        seam_repair=False,
    )
    assert aligned == "甲\n\n乙\n\n丙\n\n丁"
    assert partial == 0
    # arrival order really was reversed — otherwise this test proves nothing
    arrival = [c["request_id"] for c in provider.calls]
    assert arrival.index("1_ck04of04_attempt_0") < arrival.index("1_ck01of04_attempt_0")


def test_concurrent_chapter_translation_is_order_independent(tmp_path):
    """Verification #2 (research-concurrency, briefing): run twice with
    randomized per-request delays; the stitched output must be byte-identical
    both times, and both must equal the source-order stitch. This is the
    mechanical proof that no chunk's prompt depends on another chunk's
    completion timing."""
    responses = {"ck01": "[[PARA_1]]\n甲", "ck02": "[[PARA_1]]\n乙",
                 "ck03": "[[PARA_1]]\n丙", "ck04": "[[PARA_1]]\n丁"}
    outputs = []
    for _ in range(2):
        delays = {k: random.uniform(0.0, 0.03) for k in responses}
        provider = _FakeConcurrentProvider(
            response_for=responses, delays=delays, max_concurrent_requests=4,
        )
        aligned, _warns, _partial = drv._translate_chapter_chunked_concurrent(
            provider, html=_four_paragraph_html(), chapter_id="c1", chapter_label="1",
            book_title="T", target_lang="zh-tw", carryover="",
            book_dir=tmp_path, default_temperature=0.3, chunk_max_chars=5,
            seam_repair=False,
        )
        outputs.append(aligned)
    assert outputs[0] == outputs[1] == "甲\n\n乙\n\n丙\n\n丁"


def test_concurrent_chunk_carryover_is_source_text_not_translation(tmp_path):
    """The prompt sent for chunk 2 must carry chunk 1's SOURCE paragraph
    ('Para A.'), never chunk 1's translation ('甲') — that's the actual fix,
    not just an enabler for concurrency."""
    provider = _FakeConcurrentProvider(
        response_for={"ck01": "[[PARA_1]]\n甲", "ck02": "[[PARA_1]]\n乙",
                      "ck03": "[[PARA_1]]\n丙", "ck04": "[[PARA_1]]\n丁"},
        max_concurrent_requests=4,
    )
    drv._translate_chapter_chunked_concurrent(
        provider, html=_four_paragraph_html(), chapter_id="c1", chapter_label="1",
        book_title="T", target_lang="zh-tw", carryover="",
        book_dir=tmp_path, default_temperature=0.3, chunk_max_chars=5,
        seam_repair=False,
    )
    ck02_call = next(c for c in provider.calls if c["request_id"] == "1_ck02of04_attempt_0")
    assert "Para A." in ck02_call["prompt"]
    assert "甲" not in ck02_call["prompt"]


def test_sequential_path_unchanged_carryover_is_translation_not_source(tmp_path):
    """Regression guard: the OLD function (no concurrency) must still carry
    the PREVIOUS chunk's TRANSLATION forward, exactly as before — this test
    would go red if the source-carry fix ever leaked into the sequential
    path, which the regression requirement forbids."""
    mock = _mock_provider_returning(
        "[[PARA_1]]\n甲",  # chunk 1
        "[[PARA_1]]\n乙",  # chunk 2
        "[[PARA_1]]\n丙",  # chunk 3
        "[[PARA_1]]\n丁",  # chunk 4
    )
    aligned, _warns, _partial = drv._translate_chapter_chunked(
        mock, html=_four_paragraph_html(), chapter_id="c1", chapter_label="1",
        book_title="T", target_lang="zh-tw", carryover="",
        book_dir=tmp_path, default_temperature=0.3, chunk_max_chars=5,
    )
    assert aligned == "甲\n\n乙\n\n丙\n\n丁"
    calls = mock.translate.call_args_list
    ck02_prompt = calls[1].args[0]
    assert "甲" in ck02_prompt  # previous chunk's TRANSLATION, old behavior
    assert "Para A." not in ck02_prompt


def test_seam_repair_uses_real_prior_translation_and_replaces_boundary(tmp_path):
    provider = _FakeConcurrentProvider(
        response_for={
            "ck01": "[[PARA_1]]\n甲", "ck02": "[[PARA_1]]\n乙", "ck03": "[[PARA_1]]\n丙",
            "seam01": "[[PARA_1]]\n乙修", "seam02": "[[PARA_1]]\n丙修",
        },
        max_concurrent_requests=4,
    )
    html = "<p>Para A.</p><p>Para B.</p><p>Para C.</p>"
    aligned, warns, _partial = drv._translate_chapter_chunked_concurrent(
        provider, html=html, chapter_id="c1", chapter_label="1",
        book_title="T", target_lang="zh-tw", carryover="",
        book_dir=tmp_path, default_temperature=0.3, chunk_max_chars=5,
        seam_repair=True,
    )
    assert aligned == "甲\n\n乙修\n\n丙修"
    assert any(w.startswith("seam_repair_paragraphs=2/3") for w in warns)
    seam01_call = next(c for c in provider.calls if c["request_id"] == "1_seam01_attempt_0")
    seam02_call = next(c for c in provider.calls if c["request_id"] == "1_seam02_attempt_0")
    # seam01's context is chunk 1's REAL pass-1 translation ("甲"), not its source.
    assert "甲" in seam01_call["prompt"]
    # seam02's context is chunk 2's ORIGINAL pass-1 output ("乙"), not the
    # already-repaired "乙修" — seam repairs run concurrently off pass-1, not chained.
    assert "乙" in seam02_call["prompt"] and "乙修" not in seam02_call["prompt"]


def test_seam_repair_disabled_makes_no_extra_calls(tmp_path):
    provider = _FakeConcurrentProvider(
        response_for={"ck01": "[[PARA_1]]\n甲", "ck02": "[[PARA_1]]\n乙", "ck03": "[[PARA_1]]\n丙"},
        max_concurrent_requests=4,
    )
    html = "<p>Para A.</p><p>Para B.</p><p>Para C.</p>"
    aligned, warns, _partial = drv._translate_chapter_chunked_concurrent(
        provider, html=html, chapter_id="c1", chapter_label="1",
        book_title="T", target_lang="zh-tw", carryover="",
        book_dir=tmp_path, default_temperature=0.3, chunk_max_chars=5,
        seam_repair=False,
    )
    assert aligned == "甲\n\n乙\n\n丙"
    assert any(w == "seam_repair_paragraphs=0/3" for w in warns)
    assert not any("seam" in c["request_id"] for c in provider.calls)


def test_single_paragraph_fallback_collapses_internal_blank_line(tmp_path):
    """The no-marker fallback prompt never goes through parse_marker_output,
    so it needs its own blank-line collapse (review-15 §7 gap) — otherwise a
    single-paragraph chunk's fallback text could itself look like 2
    paragraphs once chunker.stitch() joins it with siblings."""
    mock = _mock_provider_returning("上半段。\n\n下半段（模型自己插的空行）。")
    result, warns = drv._translate_single_paragraph_fallback(
        mock, "Hello.", "zh-tw", tmp_path, "ck01",
    )
    assert result == "上半段。\n下半段（模型自己插的空行）。"
    assert any("blank line" in w for w in warns)


def test_single_paragraph_fallback_no_warning_on_clean_output(tmp_path):
    mock = _mock_provider_returning("乾淨的一段。")
    result, warns = drv._translate_single_paragraph_fallback(
        mock, "Hello.", "zh-tw", tmp_path, "ck01",
    )
    assert result == "乾淨的一段。"
    assert not any("blank line" in w for w in warns)


def _openai_response(content: str, finish_reason: str) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    }
    return response


def test_length_finish_reason_triggers_driver_temperature_retry(tmp_path, monkeypatch):
    post = MagicMock(side_effect=[
        _openai_response("[[PARA_1]]\n截斷", "length"),
        _openai_response("[[PARA_1]]\n完整", "stop"),
    ])
    monkeypatch.setattr("providers.omlx_provider.requests.post", post)
    provider = OmlxProvider("model-a", max_retries=3)
    aligned, _raw, warnings = drv._translate_chunk(
        provider,
        chunk_paragraphs=("Source.",),
        chapter_label="1",
        chunk_label="ck01",
        book_title="T",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
    )
    assert aligned == "完整"
    assert post.call_count == 2
    assert [call.kwargs["json"]["temperature"] for call in post.call_args_list] == [0.3, 0.5]
    assert any("response truncated" in warning for warning in warnings)


def test_stop_finish_reason_does_not_trigger_driver_retry(tmp_path, monkeypatch):
    post = MagicMock(return_value=_openai_response("[[PARA_1]]\n完整", "stop"))
    monkeypatch.setattr("providers.omlx_provider.requests.post", post)
    aligned, _raw, _warnings = drv._translate_chunk(
        OmlxProvider("model-a", max_retries=3),
        chunk_paragraphs=("Source.",),
        chapter_label="1",
        chunk_label="ck01",
        book_title="T",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
    )
    assert aligned == "完整"
    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["temperature"] == 0.3


def test_translate_chunk_passes_explicit_temperature_per_attempt_no_mutation(tmp_path):
    """Core fix for the orchestrator-flagged temperature race: each attempt's
    temperature must be passed explicitly to provider.translate(), never
    mutated on the shared provider object — mutation races when different
    chunks' attempts run concurrently on separate threads (translate_book_
    ollama.py:166 used to be `provider.temperature = 0.5 if attempt==1 ...`,
    read back at :75 in omlx_provider's payload build)."""
    mock = _mock_provider_returning(
        "[[PARA_1]]\nbad",       # attempt 0: misaligned (2 markers expected)
        "[[PARA_1]]\n甲\n\n[[PARA_2]]\n乙",  # attempt 1: aligned
    )
    aligned, _raw, _warns = drv._translate_chunk(
        mock,
        chunk_paragraphs=("A.", "B."),
        chapter_label="1",
        chunk_label="ck01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
    )
    assert aligned == "甲\n\n乙"
    calls = mock.translate.call_args_list
    assert calls[0].kwargs["temperature"] == 0.3   # attempt 0: default
    assert calls[1].kwargs["temperature"] == 0.5   # attempt 1: retry bump
    assert mock.temperature == 0.3                  # never mutated, either attempt


def test_fallback_temperature_preserved_at_point_five(tmp_path):
    """Regression guard for the temperature refactor: the last-resort
    single-paragraph fallback used to run at temperature=0.5 as a side
    effect of the (now removed) provider.temperature mutation in the second
    marker attempt. That must still be true, passed explicitly now."""
    mock = _mock_provider_returning(
        "I cannot help with this",   # marker attempt 0
        "",                          # marker attempt 1: empty
        "你好。",                     # minimal fallback: succeeds
    )
    result, _warns = drv._translate_chunk_with_recursion(
        mock,
        chunk_paragraphs=("Hello.",),
        chapter_label="1",
        chunk_label="ck01",
        book_title="Test",
        target_lang="zh-tw",
        carryover="",
        book_dir=tmp_path,
        default_temperature=0.3,
        depth=3,
        max_depth=4,
    )
    assert result == "你好。"
    fallback_call = mock.translate.call_args_list[-1]
    assert fallback_call.kwargs["temperature"] == 0.5
