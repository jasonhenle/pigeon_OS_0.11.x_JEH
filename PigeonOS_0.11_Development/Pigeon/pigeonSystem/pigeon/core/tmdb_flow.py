"""TMDb lookup flow: spawn identity, match quality overlays, command parsing.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import re
import time
import tkinter as tk
import cv2
import numpy as np
from pigeon.app_state import read_app_state
from pigeon.runtime_paths import pigeon_state_dir
import sys
from pigeon.app_state import write_app_state


def _trigger_tmdb_quality_toggle_overlay(mode: str, *, tmdb_quality_overlay_mode, tmdb_quality_overlay_t0) -> None:
    tmdb_quality_overlay_mode[0] = str(mode or "")
    tmdb_quality_overlay_t0[0] = time.monotonic()


def _tmdb_quality_toggle_overlay_state(
    now_mono: float,
    *,
    tmdb_quality_overlay_mode,
    tmdb_quality_overlay_t0,
) -> tuple[tuple[int, int, int], float, str, int]:
    """Return (BGR color, alpha, caption, phase_key) for the toggle-confirmation X overlay."""
    mode = str(tmdb_quality_overlay_mode[0] or "")
    if mode not in ("flag", "undo"):
        return ((255, 255, 255), 0.0, "", 0)
    t0 = float(tmdb_quality_overlay_t0[0] or 0.0)
    dt = max(0.0, now_mono - t0)
    hold_primary = 1.0
    hold_secondary = 3.0
    fade_s = 0.8
    total = hold_primary + hold_secondary + fade_s
    if dt >= total:
        tmdb_quality_overlay_mode[0] = ""
        return ((255, 255, 255), 0.0, "", 0)
    if mode == "flag":
        c1 = (255, 255, 255)  # white first
        c2 = (0, 0, 255)  # red second
        text = "TMDB ERROR FLAGGED"
    else:
        c1 = (0, 0, 255)  # red first
        c2 = (255, 255, 255)  # white second
        text = "TMDB ERROR REPORT UNDONE"
    if dt < hold_primary:
        return (c1, 1.0, text, 1)
    if dt < (hold_primary + hold_secondary):
        return (c2, 1.0, text, 2)
    fade_t = (dt - hold_primary - hold_secondary) / max(1e-6, fade_s)
    alpha = max(0.0, 1.0 - float(fade_t))
    return (c2, alpha, text, 3)


def _blend_tmdb_quality_toggle_overlay(
    frame_bgr: np.ndarray,
    *,
    color_bgr: tuple[int, int, int],
    alpha: float,
    caption: str,
) -> None:
    if frame_bgr is None or frame_bgr.size == 0:
        return
    a = max(0.0, min(1.0, float(alpha)))
    if a <= 1e-6:
        return
    h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
    if h < 8 or w < 8:
        return
    overlay = frame_bgr.copy()
    margin = max(16, int(round(min(w, h) * 0.24)))
    x0, y0 = margin, margin
    x1, y1 = max(x0 + 1, w - margin), max(y0 + 1, h - margin)
    thick = max(6, int(round(min(w, h) * 0.02)))
    cv2.line(overlay, (x0, y0), (x1, y1), color_bgr, thickness=thick, lineType=cv2.LINE_AA)
    cv2.line(overlay, (x0, y1), (x1, y0), color_bgr, thickness=thick, lineType=cv2.LINE_AA)
    if caption:
        fs = max(0.7, min(2.6, float(min(w, h)) / 520.0))
        txt_th = max(1, int(round(thick * 0.35)))
        tw, th = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, fs, txt_th)[0]
        tx = max(8, (w - tw) // 2)
        ty = min(h - 10, y1 + max(22, int(round(0.08 * h))))
        cv2.putText(
            overlay,
            caption,
            (tx, ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            fs,
            color_bgr,
            txt_th,
            cv2.LINE_AA,
        )
    cv2.addWeighted(overlay, a, frame_bgr, 1.0 - a, 0.0, dst=frame_bgr)


def _blend_tmdb_quality_flag_badge(frame_bgr: np.ndarray) -> None:
    """Persistent bottom-right red X while the TMDb quality flag is active."""
    if frame_bgr is None or frame_bgr.size == 0:
        return
    h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
    if h < 16 or w < 16:
        return
    size = max(16, int(round(min(w, h) * 0.06)))
    pad = max(10, int(round(min(w, h) * 0.025)))
    x1 = w - pad
    y1 = h - pad
    x0 = max(0, x1 - size)
    y0 = max(0, y1 - size)
    thick = max(2, int(round(size * 0.22)))
    # Draw directly on the destination frame to avoid per-tick full-frame copies.
    cv2.line(frame_bgr, (x0, y0), (x1, y1), (0, 0, 255), thickness=thick, lineType=cv2.LINE_AA)
    cv2.line(frame_bgr, (x0, y1), (x1, y0), (0, 0, 255), thickness=thick, lineType=cv2.LINE_AA)


def parse_tmdb_command_phrase(phrase: str) -> tuple[str, str]:
    """Return (query, prefer) with prefer one of auto | movie | tv."""
    p = phrase.strip()
    m_tv = re.match(r"(?i)^tv\s+(.+)$", p)
    if m_tv:
        return m_tv.group(1).strip(), "tv"
    m_mov = re.match(r"(?i)^movie\s+(.+)$", p)
    if m_mov:
        return m_mov.group(1).strip(), "movie"
    return p, "auto"


def _escape_log_field(s: str | None) -> str:
    return (s or "").replace("\\", "\\\\").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _clear_tmdb_missing_art(*, apple_tv_auto_state, tmdb_error_flag_retry_active) -> None:
    apple_tv_auto_state["tmdb_missing_art"] = False
    apple_tv_auto_state["tmdb_exhausted_identity"] = None
    tmdb_error_flag_retry_active[0] = False


def _cancel_tmdb_quality_auto_unlog_timer(*, root, tmdb_quality_auto_unlog_after_id) -> None:
    aid = tmdb_quality_auto_unlog_after_id[0]
    if aid:
        try:
            root.after_cancel(aid)
        except tk.TclError:
            pass
    tmdb_quality_auto_unlog_after_id[0] = None


def _tmdb_match_tier_acceptable(query: str, tier: int) -> bool:
    try:
        from pigeon.tmdb_poster import _literal_min_acceptable_tier
    except ImportError:
        return int(tier) >= 4
    return int(tier) >= int(_literal_min_acceptable_tier(query))


def _format_tmdb_match_quality_glance(s: int, f: int) -> str:
    tot = s + f
    if tot <= 0:
        return "TMDb match quality  —  no scored events yet"
    ok_pct = 100.0 * float(s) / float(tot)
    return f"TMDb match quality   {s} ok   {f} fail   {tot} events   {ok_pct:.0f}% ok"


def _refresh_match_quality_glance_label() -> None:
    # Legacy Tk glance label — unused with settings_main.
    return


def _tmdb_duration_bucket(*, _player_duration_for_tmdb) -> int | None:
    dur = _player_duration_for_tmdb()
    if dur is None:
        return None
    return int(round(float(dur) / 120.0))


def _tmdb_spawn_identity(query: str, prefer: str, *, _tmdb_duration_bucket) -> tuple[str, str, int | None]:
    try:
        from pigeon.tmdb_poster import refine_tmdb_search_query

        refined = refine_tmdb_search_query(query) or str(query or "").strip()
    except ImportError:
        refined = str(query or "").strip()
    pref = str(prefer or "auto").strip().lower()
    if pref not in ("auto", "tv", "movie"):
        pref = "auto"
    return (refined, pref, _tmdb_duration_bucket())


def _tmdb_spawn_identity_changed(
    query: str,
    prefer: str,
    metadata: dict[str, object] | None = None,
    *,
    prev_content_key: object | None = None,
    _content_key_from_metadata,
    _tmdb_spawn_identity,
    apple_tv_auto_state,
) -> bool:
    """True when this poll should start a new TMDb worker (equivalent-aware).

    ``prev_content_key`` must be the content_key from *before* this poll
    updates ``apple_tv_auto_state["content_key"]``. Reading the live state
    key here poisons the title-suffix guard (prev and new look identical).
    """
    new_id = _tmdb_spawn_identity(query, prefer)
    prev = apple_tv_auto_state.get("tmdb_key")
    if prev == new_id:
        return False
    if prev and isinstance(prev, tuple) and len(prev) >= 2:
        prev_bucket = prev[2] if len(prev) > 2 else None
        new_bucket = new_id[2] if len(new_id) > 2 else None
        if prev_bucket != new_bucket:
            return True
        try:
            from pigeon.tmdb_poster import equivalent_tmdb_search_queries

            pq, pp = str(prev[0]), str(prev[1])
            if pp == new_id[1] and equivalent_tmdb_search_queries(new_id[0], pq):
                return False
            # HBO/Max: pyatv query may alternate show name vs episode title while
            # candidates still cover the same series — do not re-fetch every poll.
            if metadata is not None:
                from pigeon.raw_title import tmdb_query_candidates_from_metadata

                for cand in tmdb_query_candidates_from_metadata(metadata):
                    if equivalent_tmdb_search_queries(cand, pq):
                        return False
                # Prefer the caller-supplied prior key; fall back only if missing.
                prev_ck = str(
                    prev_content_key
                    if prev_content_key is not None
                    else apple_tv_auto_state.get("content_key")
                    or ""
                )
                new_ck = _content_key_from_metadata(metadata) or ""
                if prev_ck and new_ck and prev_ck == new_ck:
                    return False
                if prev_ck and new_ck:
                    pt = prev_ck.rsplit("|", 1)[-1]
                    nt = new_ck.rsplit("|", 1)[-1]
                    if pt and pt == nt:
                        return False
        except ImportError:
            pass
    return True


def _tmdb_pref_from_metadata(metadata: dict[str, object]) -> str:
    prefer = str(metadata.get("prefer") or "auto").strip().lower()
    if prefer not in ("auto", "tv", "movie"):
        prefer = "auto"
    try:
        from pigeon.tmdb_poster import prefer_media_for_streaming_service

        prefer = prefer_media_for_streaming_service(
            prefer,
            app_name=str(metadata.get("app_name") or "") or None,
            app_id=str(metadata.get("app_id") or "") or None,
        )
    except ImportError:
        pass
    return prefer


def _append_tmdb_quality_event_report_log(
    *,
    outcome: str,
    title_key: str | None,
    display_title: str | None,
    msg_m: str,
    _escape_log_field, apple_tv_auto_state, streaming_badge_state,
) -> None:
    """Append one scored TMDb quality event line (SUCCESS/FAILURE) with metadata context."""
    log_p = pigeon_state_dir() / "tmdb_quality_event_reports.log"
    log_p.parent.mkdir(parents=True, exist_ok=True)
    md_raw = apple_tv_auto_state.get("last_metadata")
    raw_bits: list[str] = []
    app_bits: list[str] = []
    if isinstance(md_raw, dict):
        app_name = str(md_raw.get("app_name") or "").strip()
        app_id = str(md_raw.get("app_id") or "").strip()
        if app_name:
            app_bits.append(f'app_name="{_escape_log_field(app_name)}"')
        if app_id:
            app_bits.append(f'app_id="{_escape_log_field(app_id)}"')
        try:
            from pigeon.raw_title import raw_title_from_metadata_dict

            rt = raw_title_from_metadata_dict(md_raw)
            if (rt.raw_title or "").strip():
                raw_bits.append(f'raw_title="{_escape_log_field(rt.raw_title)}"')
            if (rt.raw_series_name or "").strip():
                raw_bits.append(f'raw_series_name="{_escape_log_field(rt.raw_series_name)}"')
            if (rt.raw_query or "").strip():
                raw_bits.append(f'raw_query="{_escape_log_field(rt.raw_query)}"')
            if (rt.raw_episode_title or "").strip():
                raw_bits.append(f'raw_episode_title="{_escape_log_field(rt.raw_episode_title)}"')
            if (rt.layer_series_title or "").strip():
                raw_bits.append(f'layer_series_title="{_escape_log_field(rt.layer_series_title)}"')
            if not raw_bits:
                raw_bits.append("(rawTitle layers empty for this snapshot)")
        except Exception:
            t_fallback = str(md_raw.get("title") or "").strip()
            q_fallback = str(md_raw.get("query") or "").strip()
            raw_bits.append(
                f'fallback_title="{_escape_log_field(t_fallback)}" query="{_escape_log_field(q_fallback)}"'
            )
    else:
        raw_bits.append("(no last_metadata dict)")
    sb_label = str(streaming_badge_state.get("label") or "").strip()
    sb_filename = str(streaming_badge_state.get("filename") or "").strip()
    if sb_label:
        app_bits.append(f'streaming_service_label="{_escape_log_field(sb_label)}"')
    if sb_filename:
        app_bits.append(f'streaming_service_badge="{_escape_log_field(sb_filename)}"')
    if not app_bits:
        app_bits.append("(streaming_service unknown)")
    fetch_head = (msg_m or "").split("::", 1)[0].strip()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    out_u = str(outcome or "").strip().upper()
    if out_u not in ("SUCCESS", "FAILURE"):
        out_u = "UNKNOWN"
    line = (
        f"{ts}\t{out_u}\t"
        f'tmdb_title_key="{_escape_log_field(title_key or "")}"\t'
        f'display_title="{_escape_log_field(display_title or "")}"\t'
        f'fetch_summary_head="{_escape_log_field(fetch_head)}"\t'
        f"{' '.join(app_bits)}\t"
        f"{' '.join(raw_bits)}\n"
    )
    with log_p.open("a", encoding="utf-8") as lf:
        lf.write(line)
    try:
        from pigeon.tmdb_desktop_report import append_tmdb_quality_row

        append_tmdb_quality_row(
            outcome=out_u,
            title_key=title_key,
            display_title=display_title,
            fetch_summary_head=fetch_head,
            app_streaming_context=" ".join(app_bits),
            raw_title_context=" ".join(raw_bits),
        )
    except Exception:
        pass
    if out_u == "FAILURE":
        try:
            from pigeon.tmdb_desktop_report import append_tmdb_error_event

            append_tmdb_error_event(
                last_metadata=md_raw if isinstance(md_raw, dict) else None,
                streaming_badge_state=streaming_badge_state,
            )
        except Exception:
            pass


def _mark_tmdb_missing_art(*, identity: object | None = None, apple_tv_auto_state, tmdb_error_flag_retry_active) -> None:
    """Stop empty-display poll respawns and show the circles '?' placeholder."""
    apple_tv_auto_state["tmdb_missing_art"] = True
    if identity is not None:
        apple_tv_auto_state["tmdb_exhausted_identity"] = identity
        apple_tv_auto_state["tmdb_key"] = identity
    tmdb_error_flag_retry_active[0] = False
    try:
        sys.stderr.write(
            "pigeon: TMDb give-up — showing missing-art placeholder (no further auto-retries).\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _read_tmdb_quality_counts(*, _PIGEON_EXT) -> tuple[int, int]:
    if not _PIGEON_EXT:
        return (0, 0)
    try:
        st = read_app_state()
        s = int(st.get("tmdb_quality_successes", 0) or 0)
        f = int(st.get("tmdb_quality_failures", 0) or 0)
        return (s, f)
    except Exception:
        return (0, 0)


def _adjust_tmdb_quality_failure_delta(delta: int, *, _PIGEON_EXT, _refresh_match_quality_glance_label, match_quality_glance_sig) -> None:
    """Persist ±1 failure immediately (⌘⇧X flag on / undo); refreshes Settings glance."""
    if not _PIGEON_EXT or int(delta) == 0:
        return
    try:
        st = read_app_state()
        s = int(st.get("tmdb_quality_successes", 0) or 0)
        f = max(0, int(st.get("tmdb_quality_failures", 0) or 0) + int(delta))
        write_app_state(tmdb_quality_successes=s, tmdb_quality_failures=f)
        match_quality_glance_sig[0] = ""
        _refresh_match_quality_glance_label()
    except Exception:
        pass


def on_reset_tmdb_match_quality_stats(*, _PIGEON_EXT, _refresh_match_quality_glance_label, match_quality_glance_sig) -> None:
    """Zero the Settings success/fail counters (state.json only). Logs and desktop reports unchanged."""
    if not _PIGEON_EXT:
        return
    try:
        write_app_state(tmdb_quality_successes=0, tmdb_quality_failures=0)
        match_quality_glance_sig[0] = ""
        _refresh_match_quality_glance_label()
    except Exception:
        pass
