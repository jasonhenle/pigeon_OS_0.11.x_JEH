import argparse
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# Tk must initialize before OpenCV on macOS: cv2 pulls X11/SDL dylibs that otherwise break Tcl (Tcl_InitNotifier → abort).
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import tkinter.scrolledtext as scrolledtext
import tkinter.simpledialog as simpledialog

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageTk
import cv2

# Status bar: one black pill cols 3–17, bar-shaped mask hole + translucent bar; rows 6–8 gradient.
# Remaining label spans cols 16–17 (was 17–18) so the right edge has one extra column of margin.
TRT_DISPLAY_ROW = 8.0  # nowPlaying band bottom aligns to row 9 (baseline of canvas)
TRT_PLAYED_COL = 3  # TRTPlayed (elapsed)
TRT_PLAYED_TEXT = "0"
TRT_REMAINING_COL = 16  # TRTRemaining (countdown); 2-wide → ends at col 17 (col 19 = breathing room)
TRT_REMAINING_TEXT = "1:00:00"
TRT_LABEL_SPAN_W = 2
TRT_LABEL_SPAN_H = 1
# Apple TV auto-poll; TRT labels on a steady ~1 Hz metronome (see _playback_ui_tick).
APPLE_TV_POLL_MS = 3000
APPLE_TV_IDLE_POLL_MS = 6000
APPLE_TV_FAIL_POLL_MAX_MS = 15000
RECEIVER_POLL_MS = 750
RECEIVER_VOLUME_POLL_MS = 500
PLAYBACK_UI_TICK_MS = 1000  # fallback first delay only; actual spacing uses monotonic deadlines
# Title / TMDb logo (views 1/3/5; not view 2 visualizer): 5×2 top-right at grid (1.5, 13); top-aligned in cell.
TMDB_LOGO_ANCHOR_ROW = 1.5
TMDB_LOGO_TOP_RIGHT_COL = 13.0
TMDB_LOGO_SPAN_W = 5
TMDB_LOGO_SPAN_H = 2
TMDB_LOGO_FIT_SCALE = 0.88
# View 6 logo constraints: grow as large as possible while staying inside rows 2..5 and cols 2..18.
TMDB_LOGO_VIEW6_ANCHOR_ROW = 2
TMDB_LOGO_VIEW6_ANCHOR_COL = 2
TMDB_LOGO_VIEW6_SPAN_W = 17
TMDB_LOGO_VIEW6_SPAN_H = 4
TMDB_LOGO_VIEW6_FIT_SCALE = 1.0
VIEW_ONE_BADGE_COL_RIGHT = 4.0  # 2-wide badge left-aligns to grid column 2
VIEW_ONE_CLOCK_COL_RIGHT = 19.0  # clock right edge aligns to the right side of column 18
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

_PROJECT_DIR = _CODE_DIR
if not os.path.isdir(os.path.join(_PROJECT_DIR, "pigeonAssets")):
    _parent = os.path.dirname(_PROJECT_DIR)
    if os.path.isdir(os.path.join(_parent, "pigeonAssets")):
        _PROJECT_DIR = _parent

from pigeon.app_state import (
    merge_legacy_saved_receivers_into_av_slot,
    migrate_device_slots_from_legacy_if_needed,
    read_app_state,
    read_last_apple_tv,
    read_last_receiver,
    read_saved_av_receiver,
    read_saved_streaming_device,
    write_saved_game,
    write_saved_other,
    write_saved_projector,
    write_saved_tv,
    write_app_state,
    write_last_receiver,
)
from pigeon.media_folders import (
    consolidate_legacy_pigeondata_media_folders,
)
from pigeon.compositing import cv_resize_interp
from pigeon.stage_background import bgr_to_tk_hex, get_stage_bgr
from pigeon.tmdb_tt_contrast import GRADIENT_BGR_DARK
from pigeon.version import version_string
from pigeon.core.binding import bind_deps as _bind_deps
from pigeon.core.binding import late as _late
from pigeon.core import settings_ui as _core_settings_ui
from pigeon.core import saver_state as _core_saver_state
from pigeon.core import now_playing as _core_now_playing
from pigeon.core import stage_render as _core_stage_render
from pigeon.core import view_four as _core_view_four
from pigeon.core import tmdb_flow as _core_tmdb_flow
from pigeon.core import input_keys as _core_input_keys
from pigeon.core import device_control as _core_device_control
from pigeon.core import pairing as _core_pairing
from pigeon.core import startup as _core_startup
from pigeon.core import view_one as _core_view_one

try:
    from pigeon.tmdb_retry_log import append_entry as _tmdb_retry_log_append
    from pigeon.tmdb_retry_log import read_tail_lines as _tmdb_retry_log_read_tail
except ImportError:

    def _tmdb_retry_log_append(_entry: dict) -> None:
        pass

    def _tmdb_retry_log_read_tail(_max_lines: int = 120) -> list[str]:
        return []


def _log_optional_import_failure(group: str, exc: BaseException) -> None:
    """Report which optional UI group failed without silencing the root cause."""
    import traceback

    name = getattr(exc, "name", None) or ""
    detail = f"{type(exc).__name__}: {exc}"
    if name:
        detail = f"{detail} (module={name})"
    msg = f"pigeon: optional import group {group!r} disabled — {detail}"
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        sys.stderr.flush()
    except Exception:
        pass
    try:
        from pigeon.pi_diagnostics import append_pigeon_log

        append_pigeon_log(msg)
    except Exception:
        pass


# Optional UI extension groups. A failure disables only that group.
_PIGEON_EXT = False
alpha_blend_bgra_over_bgr = None  # type: ignore[misc, assignment]
lerp_bgr_red_monochrome = None  # type: ignore[misc, assignment]
scale_bgra_rgb = None  # type: ignore[misc, assignment]
scale_height_and_center_crop = None  # type: ignore[misc, assignment]
scale_cover_center_crop = None  # type: ignore[misc, assignment]
scale_uniform_letterbox = None  # type: ignore[misc, assignment]
rect_for_span_at_cell = None  # type: ignore[misc, assignment]
rect_for_span_top_right_at_cell = None  # type: ignore[misc, assignment]
get_grid_geometry = None  # type: ignore[misc, assignment]
playback_lower_gradient_bgra = None  # type: ignore[misc, assignment]
DESIGN_W = DESIGN_H = 0
blend_overlay_bgr = None  # type: ignore[misc, assignment]
build_stage_overlay_source_bgra = None  # type: ignore[misc, assignment]
ClockCalendarWidget = None  # type: ignore[misc, assignment]
CLOCK_ANCHOR_ROW = 1.0
CLOCK_ANCHOR_COL = int(VIEW_ONE_CLOCK_COL_RIGHT)
LOCATION_TOAST_FULL_S = 15.0
LOCATION_TOAST_FADE_S = 2.0
location_toast_patch_bgra = None  # type: ignore[misc, assignment]
TmdbLogoWidget = None  # type: ignore[misc, assignment]
ViewOneVariant = None  # type: ignore[misc, assignment]
resolve_view_one_variant = None  # type: ignore[misc, assignment]
variant_has_alternate = None  # type: ignore[misc, assignment]
variant_uses_full_path = None  # type: ignore[misc, assignment]
render_ui_text_patch_bgra = None  # type: ignore[misc, assignment]
render_view_one_video_content_b_title_patch_bgra = None  # type: ignore[misc, assignment]
render_ui_music_text_patch_bgra = None  # type: ignore[misc, assignment]
load_pigeon_temp_logo_bgra = None  # type: ignore[misc, assignment]
StatusBarWidget = None  # type: ignore[misc, assignment]
clock_saver_composite_bgra = None  # type: ignore[misc, assignment]
ClockSaverVolumeHold = None  # type: ignore[misc, assignment]
VolumeLineReveal = None  # type: ignore[misc, assignment]
audio_meter_face_enabled = None  # type: ignore[misc, assignment]
latest_meter_cache_key = None  # type: ignore[misc, assignment]
render_audio_meter_composite_bgra = None  # type: ignore[misc, assignment]
stop_audio_meter_capture = None  # type: ignore[misc, assignment]
sync_audio_meter_capture = None  # type: ignore[misc, assignment]
latest_visualizer_cache_key = None  # type: ignore[misc, assignment]
audio_connection_ok = None  # type: ignore[misc, assignment]
program_audio_present = None  # type: ignore[misc, assignment]
program_audio_session_present = None  # type: ignore[misc, assignment]
toggle_audio_meter_face = None  # type: ignore[misc, assignment]
PlaybackOverlayWidget = None  # type: ignore[misc, assignment]
compose_playback_volume_widget_line = None  # type: ignore[misc, assignment]
PATCH_LAYER_RECEIVER_AUDIO = "receiver_audio"  # type: ignore[misc, assignment]
PATCH_LAYER_STREAMING_BADGE = "streaming_badge"  # type: ignore[misc, assignment]
pigeon_wordmark_design_patch = None  # type: ignore[misc, assignment]
ViewCirclesWidget = None  # type: ignore[misc, assignment]
MainSettingsWidget = None  # type: ignore[misc, assignment]
build_info_cluster_design_patches = None  # type: ignore[misc, assignment]
INFO_CLUSTER_COL_RIGHT = 18.0
INFO_CLUSTER_CLOCK_ROW_1BASED = 1.0  # type: ignore[misc, assignment]
prepare_default_poster_at_startup = None  # type: ignore[misc, assignment]
metadata_has_playback_title = None  # type: ignore[misc, assignment]
resolve_metadata_tmdb_query = None  # type: ignore[misc, assignment]

# Splash symbols stay importable when splash group fails (call sites check paths).
FALLBACK_SPLASH_FRAME_COUNT = 0
SPLASH_FADE_OUT_FRAMES = 0
SPLASH_CLOCK_REVEAL_FRAME = 90
SPLASH_FPS = 30
SPLASH_MAX_DURATION_S = 0.0
apply_splash_global_alpha = None  # type: ignore[misc, assignment]
bgra_to_pil_rgba = None  # type: ignore[misc, assignment]
builtin_splash_bgra_frame = None  # type: ignore[misc, assignment]
composite_splash_over_bg = None  # type: ignore[misc, assignment]
find_splash_video_path = None  # type: ignore[misc, assignment]
flatten_bgra_over_bg_to_rgb = None  # type: ignore[misc, assignment]
list_splash_png_paths = None  # type: ignore[misc, assignment]
load_splash_bgra = None  # type: ignore[misc, assignment]
resolve_splash_media = None  # type: ignore[misc, assignment]
resize_bgra_if_needed = None  # type: ignore[misc, assignment]
splash_end_fade_factor = None  # type: ignore[misc, assignment]
splash_effective_frame_count = None  # type: ignore[misc, assignment]
splash_keep_alpha_for_live_clock = None  # type: ignore[misc, assignment]

try:
    from pigeon.compositing import (
        alpha_blend_bgra_over_bgr,
        lerp_bgr_red_monochrome,
        scale_bgra_rgb,
        scale_cover_center_crop,
        scale_height_and_center_crop,
        scale_uniform_letterbox,
    )
    from pigeon.design import (
        DESIGN_H,
        DESIGN_W,
        get_grid_geometry,
        playback_lower_gradient_bgra,
        rect_for_span_at_cell,
        rect_for_span_top_right_at_cell,
    )
    from pigeon.overlay import blend_overlay_bgr, build_stage_overlay_source_bgra

    _PIGEON_EXT = True
except ImportError as _exc:
    _log_optional_import_failure("core_compositing", _exc)

from pigeon.linux_kiosk import (
    apply_kiosk_fullscreen,
    enforce_kiosk,
    linux_kiosk_enabled,
    release_kiosk,
    schedule_kiosk_guard,
    window_covers_display,
)
from pigeon.clock_saver_policy import (
    pausesaver_hold_from_metadata_class,
    player_reports_playing,
    tick_pause_hold,
)

try:
    from pigeon.widgets.clock_calendar import (
        CLOCK_WIDGET_COL_RIGHT,
        CLOCK_WIDGET_ROW,
        ClockCalendarWidget,
    )

    CLOCK_ANCHOR_ROW = CLOCK_WIDGET_ROW
    CLOCK_ANCHOR_COL = int(VIEW_ONE_CLOCK_COL_RIGHT)
    from pigeon.widgets.location_toast import (
        LOCATION_TOAST_FADE_S,
        LOCATION_TOAST_FULL_S,
        location_toast_patch_bgra,
    )
    from pigeon.widgets.logo_tmdb import TmdbLogoWidget
    from pigeon.widgets.status_bar import StatusBarWidget
    from pigeon.widgets.clock_saver import (
        ClockSaverVolumeHold,
        VolumeLineReveal,
        clock_saver_composite_bgra,
    )
except ImportError as _exc:
    _log_optional_import_failure("clock_status_widgets", _exc)

try:
    from pigeon.widgets.audio_meter_saver import (
        audio_connection_ok,
        audio_meter_face_enabled,
        latest_meter_cache_key,
        latest_visualizer_cache_key,
        program_audio_present,
        program_audio_session_present,
        render_audio_meter_composite_bgra,
        stop_audio_meter_capture,
        sync_audio_meter_capture,
        toggle_audio_meter_face,
    )
except ImportError as _exc:
    _log_optional_import_failure("audio_meter_saver", _exc)

try:
    from pigeon.view_one_variants import (
        ViewOneVariant,
        load_pigeon_temp_logo_bgra,
        render_ui_music_text_patch_bgra,
        render_ui_text_patch_bgra,
        render_view_one_video_content_b_title_patch_bgra,
        resolve_view_one_variant,
        variant_has_alternate,
        variant_uses_full_path,
    )
except ImportError as _exc:
    _log_optional_import_failure("view_one_variants", _exc)

try:
    from pigeon.widgets.playback_overlay import (
        PATCH_LAYER_RECEIVER_AUDIO,
        PATCH_LAYER_STREAMING_BADGE,
        PlaybackOverlayWidget,
        compose_playback_volume_widget_line,
        pigeon_wordmark_design_patch,
    )
except ImportError as _exc:
    _log_optional_import_failure("playback_widgets", _exc)

try:
    from pigeon.widgets.view_circles import ViewCirclesWidget
except ImportError as _exc:
    _log_optional_import_failure("view_circles", _exc)
    ViewCirclesWidget = None  # type: ignore[misc, assignment]

try:
    from pigeon.widgets.main_settings import MainSettingsWidget
except ImportError as _exc:
    _log_optional_import_failure("main_settings", _exc)
    MainSettingsWidget = None  # type: ignore[misc, assignment]

try:
    from pigeon.widgets.info_cluster import (
        INFO_CLUSTER_CLOCK_ROW_1BASED,
        INFO_CLUSTER_COL_RIGHT,
        build_info_cluster_design_patches,
    )
    from pigeon.widgets.poster_art import prepare_default_poster_at_startup
    from pigeon.raw_title import metadata_has_playback_title, resolve_metadata_tmdb_query
except ImportError as _exc:
    _log_optional_import_failure("info_poster_metadata", _exc)

try:
    from pigeon.splash_sequence import (
        FALLBACK_SPLASH_FRAME_COUNT,
        SPLASH_CLOCK_REVEAL_FRAME,
        SPLASH_FADE_OUT_FRAMES,
        SPLASH_FPS,
        SPLASH_MAX_DURATION_S,
        apply_splash_global_alpha,
        bgra_to_pil_rgba,
        builtin_splash_bgra_frame,
        composite_splash_over_bg,
        find_splash_video_path,
        flatten_bgra_over_bg_to_rgb,
        list_splash_png_paths,
        load_splash_bgra,
        resolve_splash_media,
        resize_bgra_if_needed,
        splash_effective_frame_count,
        splash_end_fade_factor,
        splash_keep_alpha_for_live_clock,
    )
except ImportError as _exc:
    _log_optional_import_failure("splash_sequence", _exc)

if not callable(splash_keep_alpha_for_live_clock):
    def splash_keep_alpha_for_live_clock(  # type: ignore[misc]
        frame_index: int,
        reveal_frame: int = SPLASH_CLOCK_REVEAL_FRAME,
    ) -> bool:
        return int(frame_index) >= int(reveal_frame)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Native UI composes at 1280×800. Window/display target follows the panel (env override).
UI_TARGET_W = 1280
UI_TARGET_H = 800
DISPLAY_W = _env_int("PIGEON_DISPLAY_W", 1280)
DISPLAY_H = _env_int("PIGEON_DISPLAY_H", 800)
try:
    _LAUNCH_WINDOW_SCALE = float(os.environ.get("PIGEON_WINDOW_SCALE", "1.0") or "1.0")
except ValueError:
    _LAUNCH_WINDOW_SCALE = 1.0
_LAUNCH_WINDOW_SCALE = max(0.25, min(_LAUNCH_WINDOW_SCALE, 4.0))
WINDOW_W = int(round(DISPLAY_W * _LAUNCH_WINDOW_SCALE))
WINDOW_H = int(round(DISPLAY_H * _LAUNCH_WINDOW_SCALE))


def _apple_tv_scan_timeout_s() -> int:
    default = 12 if sys.platform == "linux" else 6
    return _env_int("PIGEON_APPLE_TV_SCAN_TIMEOUT", default)


def _resize_bgr_to_dims(dst_w: int, dst_h: int, src: np.ndarray) -> np.ndarray:
    """Resize to window/cap size; LANCZOS4 when upscaling, AREA when downscaling."""
    sh, sw = int(src.shape[0]), int(src.shape[1])
    if sw == dst_w and sh == dst_h:
        return src
    return cv2.resize(
        src,
        (dst_w, dst_h),
        interpolation=cv_resize_interp(sw, sh, dst_w, dst_h),
    )


def _composite_cap_dims(display_w: int, display_h: int) -> tuple[int, int, bool]:
    """
    Internal composite size for video + mic EQ + UI blits.

    Always composes at the native 1280×800 design resolution, then
    ``_present_frame_to_display`` letterboxes/pillarboxes to the live window when needed.
    """
    dw = max(1, int(display_w))
    dh = max(1, int(display_h))
    if dw == UI_TARGET_W and dh == UI_TARGET_H:
        return UI_TARGET_W, UI_TARGET_H, False
    return UI_TARGET_W, UI_TARGET_H, True


def _present_frame_to_display(
    image: np.ndarray,
    display_w: int,
    display_h: int,
    *,
    native_now_playing: bool = False,
) -> np.ndarray:
    """Scale entire frame into the window with black letterbox/pillarbox bars.

    Applies pixel-aspect compensation when the panel PAR is non-square (e.g. official
    Pi 7″ touchscreen) so designed circles stay round on glass.

    Leftover 800×480 screens get a red square at (0, 0). Native 1280×800
    frames (now-playing, clock saver, splash) are already at design size
    and must not be stamped.
    """
    dw = max(1, int(display_w))
    dh = max(1, int(display_h))
    if not native_now_playing:
        try:
            from pigeon.np_layout import is_native_design_frame, stamp_legacy_update_mark

            if not is_native_design_frame(image):
                image = np.ascontiguousarray(image.copy())
                stamp_legacy_update_mark(image)
        except Exception:
            pass
    try:
        from pigeon.display_par import apply_par_compensation

        return apply_par_compensation(image, display_w=dw, display_h=dh)
    except Exception:
        pass
    if int(image.shape[1]) == dw and int(image.shape[0]) == dh:
        return image
    if _PIGEON_EXT and scale_uniform_letterbox is not None:
        return scale_uniform_letterbox(image, dw, dh)
    return _resize_bgr_to_dims(dw, dh, image)


def _bgra_to_display_window(bgra: np.ndarray) -> np.ndarray:
    """Fit splash/UI BGRA (design size) into the live display window with black bars when needed."""
    if not _PIGEON_EXT:
        return bgra
    assert resize_bgra_if_needed is not None
    fitted = resize_bgra_if_needed(bgra, UI_TARGET_W, UI_TARGET_H)
    try:
        from pigeon.display_par import apply_par_compensation

        return apply_par_compensation(fitted, display_w=WINDOW_W, display_h=WINDOW_H)
    except Exception:
        pass
    if scale_uniform_letterbox is not None:
        return scale_uniform_letterbox(fitted, WINDOW_W, WINDOW_H)
    return resize_bgra_if_needed(fitted, WINDOW_W, WINDOW_H)


# App-logo backdrop when TMDb has no art: letterbox canvas is at most this fraction of the live window.
APP_LOGO_FALLBACK_MAX_RESOLUTION_FRACTION = 0.9
# Reserved strip at bottom when developer mode is on (must not sit under the full-bleed video label).
OVERLAY_HUD_H = 52


class DevPhase(IntEnum):
    OFF = 0
    GRID = 1  # rare: Advanced matrix may stash this while open
    MAIN_SETTINGS = 2  # SVG settings_main + pigeon / prefs / update / keyboard


class DisplayView(IntEnum):
    """User-selectable display layout (keys 1, 4, 5). Unused values kept for stable ints."""

    ONE = 1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6


class ViewOneLayout(IntEnum):
    """
    When ``DisplayView.ONE`` is active, Shift+1 cycles the layout toggle (historical
    full / simple / poster modes). View 1 always renders the 070326 now-playing screen.

    Member names are **historical** — they describe the happy-path visual that
    shipped before the v0.6.14 V01 ↔ V02 swap and the v0.6.19 view-naming
    refactor. The toggle's real meaning in the new vocabulary is
    ``viewOne.videoContent_a`` (initial) vs. ``viewOne.videoContent_b``
    (alternate). The V# variant that actually renders depends on this toggle
    *and* on which content assets are live (see
    ``pigeon.view_one_variants.resolve_view_one_variant``).

    ``PIGEON_FULL`` (0, initial) → ``viewOne.videoContent_a``: default mode.
    When all TMDb assets are present renders V01 — minimal pigeonTMDB_TT on
    black. With missing assets routes to V03 / V05 / V07 / V08 / V09.
    ``PIGEON_SIMPLE`` (1) → ``viewOne.videoContent_b``: alternate mode.
    Toggled on by pressing Shift+``1`` while on view 1. When all TMDb assets are
    present renders V02 — the full pigeonTMDB_BD + pigeonTMDB_TT + appLogo
    composition. With missing assets routes to V04 / V06.

    ``PIGEON_POSTER`` (2) → ``viewOne.videoContent_c``: poster mode. Keeps the
    same chrome stack as viewOne.videoContent_b but replaces TMDb TT/BD with
    ``pigeonTMDB_Poster`` centered horizontally in the top 7 rows.

    Related view identifiers in the new vocabulary (not modeled by this enum
    because they're driven by content presence or idle timers rather than a
    user toggle):

    * ``viewOne.audioContent`` — music-only content. A MediaType.Music
      override in ``_compose_shown_frame`` substitutes a two-line text patch
      ("Track title" + "Artist - Album") inside the pigeonTMDB_TT rect and
      suppresses the bottom gradient.
    * ``viewOne.startup`` / ``viewOne.noContent`` — V09 fallback; static Pigeon logo at 30% alpha.
    * ``viewOne.clockSaver_a`` / ``viewOne.clockSaver_b`` — the idle clock
      saver composites over whichever base (black or pigeonTMDB_BD) the
      active videoContent variant produced.
    """

    PIGEON_FULL = 0
    PIGEON_SIMPLE = 1
    PIGEON_POSTER = 2


# TMDb static backdrop scene (not paused-video dim 0.3).
BACKDROP_BRIGHTNESS = 0.8
# Static landing logo (no video): full brightness; old 0.3 “paused video” level hid the art.
LANDING_DISPLAY_BRIGHTNESS = 1.0
LANDING_DIM_BRIGHTNESS = 0.78  # Space-bar pulse “off” — still readable vs old 0.3
PAUSED_SCREEN_BACKDROP_DIM = 0.72
# After UI bootstrap, optional auto-restore of saved TMDb backdrop (env-gated) runs after this delay.
STARTUP_PIGEON_WORDMARK_MAX_S = 5.0
# After splash: optional startup transition timing (no mic EQ).
SKIP_POST_SPLASH_STARTUP_TRANSITION = True
# After splash lifts: optional clock ease-up from black (0 = full-on; PNG splash reveals saver).
CLOCK_STARTUP_FADE_S = 0.0
# If True and a saved TMDb backdrop exists, switch to it when this timer elapses.
STARTUP_AUTO_RESTORE_SAVED_BACKDROP = os.environ.get("PIGEON_STARTUP_RESTORE_BACKDROP", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Theater UI: red luma-mono only when neither Apple TV nor Pigeon UI shows activity this long.
# ATV side: poll metadata deltas (see _update_atv_interaction_from_poll_metadata).
# Pigeon side: mouse / keyboard / settings scroll (see _bump_pigeon_user_activity).
# For ATV, 0 = never bumped → treated as idle until the first remote-driven signal (still needs Pigeon idle).
THEATER_IDLE_DIM_AFTER_S = 45.0
# Red idle-dim is **off** by default until an ambient-light (or other) trigger is integrated.
# Re-enable anytime: ``PIGEON_THEATER_IDLE_DIM=1`` (or ``true`` / ``yes`` / ``on``). Explicit ``0`` / ``off`` keeps it off.
_THEATER_IDLE_DIM_ENV = os.environ.get("PIGEON_THEATER_IDLE_DIM", "").strip().lower()
THEATER_IDLE_DIM_ENABLED = (
    _THEATER_IDLE_DIM_ENV in ("1", "true", "yes", "on")
    if _THEATER_IDLE_DIM_ENV
    else False
)
# Ease in/out between full color and red luma-mono when idle-dim target changes.
ATV_IDLE_MONO_ANIM_S = 2.0
# Idle “clock saver” (screensaver): requires both ``CLOCK_SAVER_AFTER_S`` of **UI** inactivity (mouse/keys/etc.;
# mic does not touch those timestamps) **and** the same span without **significant** device signals (content
# selection, ``device_state``, or ``volume_percent`` from the player poll, plus receiver volume line changes).
CLOCK_SAVER_AFTER_S = 300.0
# Leave settings_main after this long with no mouse/key/rotary input.
SETTINGS_MENU_IDLE_EXIT_S = 60.0
# Saver text opacity when idle; tap while saver is up briefly uses 1.0 (see clock_saver_peek_until_mono).
CLOCK_SAVER_DIM_OPACITY = 0.54
CLOCK_SAVER_PEEK_S = 2.5
# With a TMDb backdrop visible: fade backdrop to black under the clock after this idle span on the saver.
# Clock saver: multiply backdrop RGB by this factor (0.3 → 30% brightness) while saver is active.
CLOCK_SAVER_BACKDROP_DIM = 0.3
# Position-stall grace: while content position has advanced within this window, the clock-saver
# "device signal" timestamp keeps being bumped to ``now``, so the 300 s saver timer stays reset.
# Once the reported playback position stays flat for longer than this, the timer resumes counting
# from the last advance toward ``CLOCK_SAVER_AFTER_S``. Tuned to survive short buffering stalls
# (typical re-buffer is < 2 s) without prematurely arming the saver. A position advance while the
# saver is already open ends it on the next render tick via the same bump.
CLOCK_SAVER_POSITION_STALL_GRACE_S = 5.0
# When player poll metadata stays unchanged for this long **and** no local Pigeon
# controls were used for the same span, force the clock saver on (independent of the
# 300 s UI+device idle path). Position not advancing is the primary signal; HDMI
# frame/title changes also refresh the stamp. When metadata is driving the saver,
# the HDMI unchanged-frame streak rule is ignored.
CLOCK_SAVER_METADATA_IDLE_AFTER_S = 120.0
# Pause-hold span is CLOCK_SAVER_PAUSED_AFTER_S (clock_saver_policy).

# Idle composite cadence when video is paused (lower than live playback).
PAUSED_COMPOSITE_MS = 83  # ~12 FPS

HOTKEY_BINDTAG = "Pigeon0_5_hotkeys"

def _paint_boolean_led(canvas: tk.Canvas, ok: bool | None) -> None:
    """Single lamp: green=detected+compatible, amber=detected+unknown/incompatible, red=not detected/not compatible."""
    canvas.delete("all")
    if ok is True:
        fill = "#1fcb5d"
    elif ok is None:
        fill = "#f0ad4e"
    else:
        fill = "#e74c3c"
    canvas.create_oval(2, 2, 14, 14, fill=fill, outline="#151518", width=1)


def _prepend_hotkey_bindtag(widget: tk.Misc, tag: str = HOTKEY_BINDTAG) -> None:
    widget.bindtags((tag,) + widget.bindtags())


def _widget_accepts_typing(widget: tk.Misc) -> bool:
    """True if Return/other keys should go to the widget (not global developer-mode shortcuts)."""
    try:
        cls = widget.winfo_class()
    except tk.TclError:
        return False
    if cls == "Text":
        try:
            if str(widget.cget("state")).lower() == "disabled":
                return False
        except tk.TclError:
            pass
        return True
    if cls in ("Entry",):
        return True
    if cls == "TEntry" or cls == "TCombobox":
        return True
    return False


def _raw_title_query_from_metadata(md: dict | None) -> str | None:
    """Verbatim playback ``title`` (rawTitle layer) refined for TMDb."""
    if not md:
        return None
    try:
        from pigeon.raw_title import raw_title_from_metadata_dict
        from pigeon.tmdb_poster import is_degenerate_tmdb_query, refine_tmdb_search_query

        rt = raw_title_from_metadata_dict(md)
        raw = (rt.raw_title or "").strip()
        if not raw:
            return None
        r = refine_tmdb_search_query(raw) or raw
        return r.strip() if r.strip() and not is_degenerate_tmdb_query(r) else None
    except Exception:
        raw = str(md.get("title") or "").strip()
        return raw or None


def _alternate_tmdb_query_from_metadata(md: dict | None, primary: str) -> str | None:
    """
    Pick a different TMDb query from last Apple TV metadata.

    Order prefers ``series_name`` / ``artist`` before ``title`` (title is often ``Sketch - SNL``).
    Refined strings that mean the same show as the primary (e.g. ``SNL`` vs ``Papyrus - SNL``)
    are skipped so ``?`` retry does not replace a good match with a sketch line.
    """
    if not primary or not md:
        return None
    try:
        from pigeon.tmdb_poster import equivalent_tmdb_search_queries, refine_tmdb_search_query
    except ImportError:

        def refine_tmdb_search_query(x: str | None) -> str | None:  # type: ignore[misc]
            return (str(x).strip() or None) if x else None

        def equivalent_tmdb_search_queries(a: str, b: str) -> bool:  # type: ignore[misc]
            return (a or "").strip().lower() == (b or "").strip().lower()

    pr = refine_tmdb_search_query(primary.strip()) or primary.strip()
    for key in ("series_name", "artist", "album", "title"):
        c = str(md.get(key) or "").strip()
        if not c:
            continue
        cr = refine_tmdb_search_query(c) or c
        if equivalent_tmdb_search_queries(cr, pr):
            continue
        return cr
    return None


def _pyatv_install_hint() -> str:
    exe = sys.executable or "python3"
    return f"Apple TV support requires pyatv.\n\nInstall with:\n  {exe} -m pip install pyatv"


@dataclass(frozen=True)
class SceneFit:
    target_w: int = WINDOW_W
    target_h: int = WINDOW_H

    def scale_and_crop(self, frame_bgr: np.ndarray) -> np.ndarray:
        if frame_bgr is None or frame_bgr.size == 0:
            raise ValueError("Empty frame")

        src_h, src_w = frame_bgr.shape[:2]
        if src_h <= 0 or src_w <= 0:
            raise ValueError(f"Invalid frame size {src_w}x{src_h}")

        scale = self.target_h / float(src_h)
        scaled_w = int(round(src_w * scale))
        scaled_h = self.target_h

        resized = cv2.resize(
            frame_bgr,
            (scaled_w, scaled_h),
            interpolation=cv_resize_interp(src_w, src_h, scaled_w, scaled_h),
        )

        if scaled_w == self.target_w:
            return resized

        if scaled_w < self.target_w:
            pad = self.target_w - scaled_w
            left = pad // 2
            right = pad - left
            sb, sg, sr = get_stage_bgr()
            return cv2.copyMakeBorder(
                resized,
                top=0,
                bottom=0,
                left=left,
                right=right,
                borderType=cv2.BORDER_CONSTANT,
                value=(sb, sg, sr),
            )

        x0 = (scaled_w - self.target_w) // 2
        x1 = x0 + self.target_w
        return resized[:, x0:x1]


def _default_render_fps() -> float:
    """Tk timer cadence for static landing + composited widgets (no scene video)."""
    import os
    import sys

    env = os.environ.get("PIGEON_UI_FPS", "").strip()
    if env:
        try:
            return max(5.0, min(60.0, float(env)))
        except ValueError:
            pass
    # Pi / Linux: keep PhotoImage uploads and settings SVG compositing lighter.
    if sys.platform.startswith("linux"):
        return 12.0
    return 30.0


def _build_landing_design_bgr(design_w: int, design_h: int, logo_path: Path | None) -> np.ndarray:
    """Pure black design-sized canvas with the Pigeon logo centered (alpha-aware)."""
    out = np.zeros((design_h, design_w, 3), dtype=np.uint8)
    if logo_path is None or not logo_path.is_file():
        sys.stderr.write("pigeon: landing logo not found — using black screen only\n")
        sys.stderr.flush()
        return out
    arr: np.ndarray | None = None
    try:
        arr = cv2.imread(str(logo_path), cv2.IMREAD_UNCHANGED)
    except Exception:
        arr = None
    if arr is None or arr.size == 0:
        try:
            pil_img = Image.open(logo_path).convert("RGBA")
            arr = np.asarray(pil_img, dtype=np.uint8)
        except Exception as e:
            sys.stderr.write(f"pigeon: could not load landing logo {logo_path}: {e}\n")
            sys.stderr.flush()
            return out
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    rh, rw = int(arr.shape[0]), int(arr.shape[1])
    if rw <= 0 or rh <= 0:
        return out
    max_side = int(0.55 * min(design_w, design_h))
    max_side = max(32, max_side)
    scale = min(max_side / float(rw), max_side / float(rh), 1.0)
    nw = max(1, int(round(rw * scale)))
    nh = max(1, int(round(rh * scale)))
    if nw != rw or nh != rh:
        arr = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
    rh, rw, ch = int(arr.shape[0]), int(arr.shape[1]), int(arr.shape[2])
    if ch >= 4:
        bgr = arr[:, :, :3]
        alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
    else:
        bgr = arr[:, :, :3]
        alpha = np.ones((rh, rw, 1), dtype=np.float32)
    x0 = (design_w - nw) // 2
    y0 = (design_h - nh) // 2
    roi = out[y0 : y0 + nh, x0 : x0 + nw]
    roi[:] = (bgr.astype(np.float32) * alpha + roi.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)
    return out


def _apply_brightness(frame_bgr: np.ndarray, factor: float) -> np.ndarray:
    factor = float(factor)
    if factor >= 0.999:
        return frame_bgr
    if factor <= 0.0:
        return np.zeros_like(frame_bgr)
    return cv2.convertScaleAbs(frame_bgr, alpha=factor, beta=0)


_TK_RGB_SCRATCH: np.ndarray | None = None


def _bgr_to_tk_image(frame_bgr: np.ndarray) -> ImageTk.PhotoImage:
    try:
        from pigeon.widgets.options_settings import apply_ui_look_bgr

        frame_bgr = apply_ui_look_bgr(frame_bgr)
    except Exception:
        pass
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)
    return ImageTk.PhotoImage(image=img)


def _update_label_photo_from_bgr(
    label: tk.Label,
    frame_bgr: np.ndarray,
    holder: list[ImageTk.PhotoImage | None],
) -> None:
    """
    Reuse one ``PhotoImage`` and ``paste`` each frame. Creating hundreds of new
    ``PhotoImage`` objects per second leaks native Tk storage and locks up after a short run.

    Pillow's ``ImageTk.PhotoImage.paste`` on the Pi takes only the image (no dest box).
    """
    try:
        from pigeon.widgets.options_settings import apply_ui_look_bgr

        frame_bgr = apply_ui_look_bgr(frame_bgr)
    except Exception:
        pass
    src = np.ascontiguousarray(frame_bgr)
    h, w = int(src.shape[0]), int(src.shape[1])
    if h < 1 or w < 1:
        return
    global _TK_RGB_SCRATCH
    rgb = _TK_RGB_SCRATCH
    if rgb is None or int(rgb.shape[0]) != h or int(rgb.shape[1]) != w:
        rgb = np.empty((h, w, 3), dtype=np.uint8)
        _TK_RGB_SCRATCH = rgb
    cv2.cvtColor(src, cv2.COLOR_BGR2RGB, dst=rgb)
    pil_img = Image.fromarray(rgb)
    ph = holder[0]
    try:
        if ph is None or ph.width() != w or ph.height() != h:
            holder[0] = ImageTk.PhotoImage(image=pil_img)
            ph = holder[0]
        else:
            ph.paste(pil_img)
    except tk.TclError:
        holder[0] = ImageTk.PhotoImage(image=pil_img)
        ph = holder[0]
    # Re-configuring the same PhotoImage every tick still stresses Tk; paste updates pixels in place.
    if getattr(label, "image", None) is not ph:
        label.configure(image=ph)
    label.image = ph


def _load_persisted_scene_enabled(default: bool = True) -> bool:
    v = read_app_state().get("scene_enabled")
    if isinstance(v, bool):
        return v
    return default


def _save_persisted_scene_enabled(enabled: bool) -> None:
    write_app_state(scene_enabled=enabled)


def _format_hmmss(seconds_value: float | int | None) -> str:
    """Compact clock for on-screen TRT: drop leading zeros / unused fields (e.g. ``30:00``, ``59:29``)."""
    try:
        total_seconds = max(0, int(float(seconds_value or 0)))
    except (TypeError, ValueError):
        total_seconds = 0
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    if minutes > 0:
        return f"{minutes}:{seconds:02d}"
    return f"{seconds}"


def main() -> int:
    sys.stderr.write(f"pigeon: running script {os.path.abspath(__file__)}\n")
    sys.stderr.flush()
    try:
        from pigeon.pi_diagnostics import run_linux_startup_checks

        run_linux_startup_checks()
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog=f"Pigeon {version_string()}", add_help=True)
    parser.parse_args()

    cap: list[cv2.VideoCapture | None] = [None]

    root = tk.Tk()
    _app_startup_mono = time.monotonic()
    try:
        root.configure(bg="#000", cursor="none")
        root.option_add("*cursor", "none")
    except tk.TclError:
        pass
    root.title("")
    root.geometry(f"{WINDOW_W}x{WINDOW_H}")
    root.minsize(
        int(round((DISPLAY_W // 2) * _LAUNCH_WINDOW_SCALE)),
        int(round((DISPLAY_H // 2) * _LAUNCH_WINDOW_SCALE)),
    )
    root.resizable(True, True)
    _kiosk_stopped_pids: list[int] = []
    _kiosk_on = bool(linux_kiosk_enabled())
    if not _kiosk_on:
        try:
            root.wm_aspect(5, 3, 5, 3)
        except tk.TclError:
            pass

    _kiosk_logged = [False]

    def _reassert_kiosk(_event: object | None = None) -> None:
        if not _kiosk_on:
            return
        try:
            updated = enforce_kiosk(root, _kiosk_stopped_pids, borderless=True)
        except Exception:
            updated = list(_kiosk_stopped_pids)
        _kiosk_stopped_pids[:] = updated
        if _kiosk_logged[0]:
            return
        try:
            covers = bool(window_covers_display(root))
        except Exception:
            covers = False
        if covers:
            _kiosk_logged[0] = True
            try:
                sys.stderr.write(
                    f"pigeon: kiosk {root.winfo_width()}x{root.winfo_height()}"
                    f"+{root.winfo_rootx()}+{root.winfo_rooty()} "
                    f"screen={root.winfo_screenwidth()}x{root.winfo_screenheight()}\n"
                )
                sys.stderr.flush()
            except Exception:
                pass

    if _kiosk_on:
        apply_kiosk_fullscreen(root, borderless=False)
        try:
            root.bind("<Map>", _reassert_kiosk)
        except tk.TclError:
            pass
        try:
            root.after(200, _reassert_kiosk)
            root.after(800, _reassert_kiosk)
        except tk.TclError:
            pass
        schedule_kiosk_guard(root, _kiosk_stopped_pids)

    def _restore_desktop_chrome() -> None:
        if _kiosk_stopped_pids or _kiosk_on:
            release_kiosk(list(_kiosk_stopped_pids))
            _kiosk_stopped_pids.clear()

    def _quit_pigeon() -> None:
        try:
            if stop_audio_meter_capture is not None:
                stop_audio_meter_capture()
        except Exception:
            pass
        _restore_desktop_chrome()
        root.quit()

    root.protocol("WM_DELETE_WINDOW", _quit_pigeon)
    if _kiosk_on:
        import atexit

        atexit.register(_restore_desktop_chrome)
    # Ensure unexpected Tk callback errors are surfaced (and don't silently kill UI behavior).
    def _report_callback_exception(exc, val, tb) -> None:  # type: ignore[no-untyped-def]
        # Tk calls this as report_callback_exception(exc, val, tb) — no bound self.
        import traceback

        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            sys.stderr.write("pigeon: Tk callback exception\n" + text + "\n")
            sys.stderr.flush()
        except Exception:
            pass
        if _kiosk_on:
            return
        try:
            messagebox.showerror("Pigeon error", text)
        except Exception:
            pass

    root.report_callback_exception = _report_callback_exception  # type: ignore[method-assign]

    shell = tk.Frame(root, bg="#111", cursor="none")
    shell.pack(fill=tk.BOTH, expand=True)
    # Main UI is built here; splash overlay sits above until the splash sequence finishes.
    content_host = tk.Frame(shell, bg="#111", cursor="none")
    content_host.pack(fill=tk.BOTH, expand=True)
    # Bridge host so the clock saver is visible the instant splash lifts — even if full
    # bootstrap has not created the real video ``Label`` yet. Bootstrap destroys this.
    _boot_clock_host = tk.Frame(content_host, bg="#000", cursor="none")
    _boot_clock_host.pack(fill=tk.BOTH, expand=True)
    _boot_clock_label = tk.Label(_boot_clock_host, bd=0, highlightthickness=0, bg="#000", cursor="none")
    _boot_clock_label.pack(fill=tk.BOTH, expand=True)
    _boot_clock_photo: list[ImageTk.PhotoImage | None] = [None]

    # Full-window splash: PNG sequence in ``pigeonSplash/`` if present, else H.264/HEVC
    # video (hardware-decoded on macOS), else built-in wordmark.
    startup_ph: list[tk.Widget | None] = [None]
    splash_png_paths: list[Path] = []
    splash_video_path: Path | None = None
    if _PIGEON_EXT:
        try:
            _assets_root = Path(_PROJECT_DIR) / "pigeonAssets"
            splash_png_paths, splash_video_path = resolve_splash_media(_assets_root)
        except Exception:
            splash_png_paths = []
            splash_video_path = None

    bootstrap_done: list[bool] = [False]
    splash_anim_done: list[bool] = [False]
    # Live underlay composited under splash PNG alpha. Stays black until frame 90.
    _splash_underlay_bgr: list[np.ndarray | None] = [None]
    # Live clock buffer (background thread); copied under the splash from frame 90.
    _splash_clock_ready_bgr: list[np.ndarray | None] = [None]
    # Stop the live-clock worker once compose owns the display.
    _splash_clock_refresh_stop: list[bool] = [False]
    # True once splash reaches ``SPLASH_CLOCK_REVEAL_FRAME``.
    _splash_reveal_clock: list[bool] = [False]
    # Registered from bootstrap: keep the real video label in sync after reveal.
    _splash_on_reveal_paint: list[object] = [None]
    _splash_underlay_paint_mono: list[float] = [0.0]
    _splash_post_hook_ran: list[bool] = [False]
    # Post-splash UI timing (splash lift).
    post_splash_mono: list[float | None] = [None]
    _post_splash_startup_hook: list[object] = [None]

    class _NullClockSaverVolumeHold:
        hold = ""
        pre_mute = ""

        def remember(self, raw, *, source="poll", hold_s=None, now=None):
            return str(raw or "")

        def pick(self, candidates, *, now=None, receiver_off=False):
            if receiver_off:
                self.hold = ""
                return ""
            for raw in candidates:
                s = str(raw or "").strip()
                if s:
                    return s
            return ""

        def is_stale_poll(self, raw, *, now=None):
            return False

        def in_nudge_grace(self, now=None):
            return False

        def display_line(self):
            return str(self.hold or "").strip()

        def clear(self):
            self.hold = ""

    _clock_saver_volume = (
        ClockSaverVolumeHold() if ClockSaverVolumeHold is not None else _NullClockSaverVolumeHold()
    )

    class _NullVolumeLineReveal:
        def note(self, raw, *, now=None):
            return None

        def opacity(self, now=None):
            return 0.0

        def fading(self, now=None):
            return False

    _volume_lines = (
        VolumeLineReveal() if VolumeLineReveal is not None else _NullVolumeLineReveal()
    )

    def _note_zone3_volume_takeover() -> None:
        """Hold the NP volume widget in zone 3 for 7s after an adjustment."""
        try:
            if view_circles_widget is not None:
                view_circles_widget.note_volume_adjustment()
        except NameError:
            pass
        except Exception:
            pass

    def _note_volume_graphics(raw: object) -> None:
        prev_label = str(getattr(_volume_lines, "last_label", "") or "")
        try:
            _volume_lines.note(raw)
        except Exception:
            pass
        # Arms and Digital-7 must track the same string. A later fat poll
        # must not paint a stale ``effective`` after this reveal.
        new_label = str(getattr(_volume_lines, "last_label", "") or "")
        if not new_label or new_label == prev_label:
            return
        if prev_label:
            _note_zone3_volume_takeover()
        try:
            if _clock_saver_volume.is_stale_poll(raw):
                return
            _clock_saver_volume.remember(raw, source="poll")
        except Exception:
            pass

    def _remember_clock_saver_volume(raw: object, *, source: str = "poll") -> str:
        """Keep the last displayable saver volume; empty / stale polls do not clear it."""
        return _clock_saver_volume.remember(raw, source=source)

    def _clock_saver_receiver_off() -> bool:
        """True when the AVR is in standby or not answering — hide the saver volume."""
        try:
            if bool(receiver_standby_holder[0]):
                return True
        except NameError:
            pass
        try:
            from pigeon.runtime_state import core_state

            rx = core_state().receiver
            if bool(getattr(rx, "standby", False)):
                return True
            if not bool(getattr(rx, "reachable", False)):
                return True
        except Exception:
            pass
        return False

    def _clock_saver_volume_raw() -> str:
        """AVR master volume for the saver + NP disc (box 3)."""
        try:
            shown = str(_clock_saver_volume.display_line() or "").strip()
            if shown:
                return shown
        except Exception:
            pass
        candidates: list[object] = []
        try:
            candidates.append(denon_vol_cache.get("effective"))
            candidates.append(denon_vol_cache.get("np_hold"))
        except NameError:
            pass
        try:
            candidates.append(receiver_overlay_state.get("volume"))
        except NameError:
            pass
        try:
            from pigeon.runtime_state import core_state

            candidates.append(core_state().receiver.volume)
        except Exception:
            pass
        try:
            st = getattr(view_circles_widget, "_state", None)
            candidates.append(getattr(st, "volume", ""))
        except NameError:
            pass
        except Exception:
            pass
        return _clock_saver_volume.pick(candidates)

    def _clock_saver_layers(**kwargs):
        try:
            from pigeon.auto_widgets import live_plan

            plan = live_plan()
        except Exception:
            plan = None
        if plan is not None:
            kwargs.setdefault("include_weather", not bool(plan.blank_weather))
            if plan.blank_volume:
                kwargs["volume"] = ""
                kwargs["line_opacity"] = 0.0
        if "volume" not in kwargs:
            kwargs["volume"] = _clock_saver_volume_raw()
        if "line_opacity" not in kwargs:
            try:
                kwargs["line_opacity"] = _volume_lines.opacity()
            except Exception:
                kwargs["line_opacity"] = 0.0
        replace = bool(kwargs.pop("replace_with_meter", False))
        if replace and render_audio_meter_composite_bgra is not None:
            op = kwargs.get("time_layer_opacity")
            if op is None:
                op = kwargs.get("layer_opacity", 1.0)
            return render_audio_meter_composite_bgra(layer_opacity=float(op))
        return clock_saver_composite_bgra(**kwargs)

    def _rasterize_clock_saver_window_bgr() -> np.ndarray | None:
        """Full-window BGR clock saver, or None."""
        if clock_saver_composite_bgra is None or alpha_blend_bgra_over_bgr is None:
            return None
        try:
            dw = int(DESIGN_W) if int(DESIGN_W) > 0 else int(UI_TARGET_W)
            dh = int(DESIGN_H) if int(DESIGN_H) > 0 else int(UI_TARGET_H)
            canvas = np.zeros((dh, dw, 3), dtype=np.uint8)
            (time_bgra, t_rect), (date_bgra, d_rect) = _clock_saver_layers(
                shadow_bgr=None,
                layer_opacity=1.0,
                time_layer_opacity=1.0,
                date_layer_opacity=1.0,
            )
            for cs_bgra, (sx, sy, sw, sh) in (
                (date_bgra, d_rect),
                (time_bgra, t_rect),
            ):
                x0 = max(0, int(sx))
                y0 = max(0, int(sy))
                x1 = min(dw, x0 + int(sw))
                y1 = min(dh, y0 + int(sh))
                if x1 <= x0 or y1 <= y0:
                    continue
                roi = canvas[y0:y1, x0:x1]
                patch = cs_bgra[0 : y1 - y0, 0 : x1 - x0]
                if patch.shape[0] != roi.shape[0] or patch.shape[1] != roi.shape[1]:
                    continue
                roi[:] = alpha_blend_bgra_over_bgr(roi, patch)
            return np.ascontiguousarray(
                _present_frame_to_display(
                    canvas, WINDOW_W, WINDOW_H, native_now_playing=True
                )
            )
        except Exception:
            return None

    def _apply_clock_to_bridge_label(shown: np.ndarray) -> None:
        """Push clock pixels onto the content_host bridge (under the splash overlay)."""
        try:
            _boot_clock_photo[0] = _bgr_to_tk_image(shown)
            if _boot_clock_label.winfo_exists():
                _boot_clock_label.configure(image=_boot_clock_photo[0])
                _boot_clock_label.image = _boot_clock_photo[0]  # type: ignore[attr-defined]
        except tk.TclError:
            pass

    def _reveal_clock_under_splash(*, refresh: bool = False) -> bool:
        """From frame 90: put the live clock into the underlay + bridge beneath splash."""
        shown = _splash_clock_ready_bgr[0]
        if shown is None or refresh:
            shown = _rasterize_clock_saver_window_bgr()
            if shown is not None:
                _splash_clock_ready_bgr[0] = shown
        if shown is None:
            return False
        # Compose owns the underlay after bootstrap; do not overwrite it with a stale prewarm.
        if not bootstrap_done[0]:
            _splash_underlay_bgr[0] = shown
            _apply_clock_to_bridge_label(shown)
            _splash_underlay_paint_mono[0] = time.monotonic()
            paint = _splash_on_reveal_paint[0]
            if callable(paint):
                try:
                    paint()
                except Exception:
                    pass
        _splash_reveal_clock[0] = True
        return True

    def _finish_post_splash_startup_transition() -> None:
        """Post-splash hook (registered from ``bootstrap``)."""
        if _splash_post_hook_ran[0]:
            return
        hook = _post_splash_startup_hook[0]
        if callable(hook):
            _splash_post_hook_ran[0] = True
            hook()

    def _try_remove_splash_overlay() -> None:
        """Destroy splash the instant the sequence ends (no bootstrap wait)."""
        if not _PIGEON_EXT:
            return
        if not splash_anim_done[0]:
            return
        w = startup_ph[0]
        if w is None:
            return
        # Clock must already be on the bridge underlay before the overlay disappears.
        _reveal_clock_under_splash(refresh=False)
        if bootstrap_done[0]:
            _finish_post_splash_startup_transition()
        try:
            w.destroy()
        except tk.TclError:
            pass
        startup_ph[0] = None
        try:
            sys.stderr.write(
                f"pigeon: splash overlay lifted +{time.monotonic() - _app_startup_mono:.3f}s\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
        # Splash frames can hold tens of MB (full-window RGB/BGRA per frame);
        # release them now that the overlay is gone. NameError guard: the caches
        # only exist when the ext splash path ran.
        try:
            _splash_rgb_cache.clear()
            _splash_bgra_cache.clear()
            _splash_photo_cache.clear()
            splash_photo[0] = None
        except NameError:
            pass
        if post_splash_mono[0] is None:
            post_splash_mono[0] = time.monotonic()

    def _live_clock_until_compose() -> None:
        """Keep the boot/video clock on wall time until compose owns the display.

        Bootstrap waits for splash, then packs widgets with ``root.update()``
        (``_splash_pump_maybe``), which lets this tick run so the saver does not
        freeze again between overlay lift and the first ``render_once``.
        """
        if bootstrap_done[0] or _splash_clock_refresh_stop[0]:
            return
        if _splash_reveal_clock[0] or splash_anim_done[0]:
            _reveal_clock_under_splash(refresh=False)
        if not bootstrap_done[0] and not _splash_clock_refresh_stop[0]:
            try:
                root.after(250, _live_clock_until_compose)
            except tk.TclError:
                pass

    _tk_pack_orig = tk.Widget.pack
    _tk_grid_orig = tk.Widget.grid
    _tk_place_orig = tk.Widget.place
    _splash_pump_next: list[float] = [0.0]

    def _splash_pump_maybe() -> None:
        if not _PIGEON_EXT or bootstrap_done[0]:
            return
        now = time.monotonic()
        if now < _splash_pump_next[0]:
            return
        _splash_pump_next[0] = now + (1.0 / 30.0)
        if (_splash_reveal_clock[0] or splash_anim_done[0]) and (
            now - float(_splash_underlay_paint_mono[0] or 0.0) >= 0.25
        ):
            _reveal_clock_under_splash(refresh=False)
        try:
            root.update()
        except tk.TclError:
            pass

    def _pack_patched(self: tk.Misc, *args: object, **kwargs: object) -> object | None:
        r = _tk_pack_orig(self, *args, **kwargs)
        _splash_pump_maybe()
        return r

    def _grid_patched(self: tk.Misc, *args: object, **kwargs: object) -> object | None:
        r = _tk_grid_orig(self, *args, **kwargs)
        _splash_pump_maybe()
        return r

    def _place_patched(self: tk.Misc, *args: object, **kwargs: object) -> object | None:
        r = _tk_place_orig(self, *args, **kwargs)
        _splash_pump_maybe()
        return r

    if _PIGEON_EXT:
        # Stay a direct child of ``shell`` (placed full-size). Do **not** pack into ``video_area`` after
        # the video ``Label``: two ``pack(..., fill=BOTH, expand=True)`` siblings leave the second with
        # zero height, so the splash would disappear. Transparent PNG / fade pixels show ``content_host``.
        splash_overlay = tk.Frame(shell, bg="#000", highlightthickness=0, bd=0, cursor="none")
        splash_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        # Placed widgets can sit under later-packed siblings (e.g. ``hud_bar``); pin above ``content_host``.
        try:
            splash_overlay.lift(content_host)
        except tk.TclError:
            try:
                splash_overlay.lift()
            except tk.TclError:
                pass
        startup_ph[0] = splash_overlay
        # Opaque black label: splash frames are always composited to RGB (never Tk alpha punch-through).
        splash_label = tk.Label(splash_overlay, bg="#000", bd=0, cursor="none")
        splash_label.pack(expand=True, fill="both")
        splash_photo: list[ImageTk.PhotoImage | None] = [None]
        splash_idx = [0]
        # Set after a lead buffer is baked so the Pi does not skip/hitch on PNG decode.
        splash_t0: list[float | None] = [None]
        _splash_wait_deadline: list[float | None] = [None]
        _splash_bg_bgr = (0, 0, 0)
        # Black underlay until frame 90 — early PNG frames are transparent and must not reveal the clock.
        _splash_underlay_bgr[0] = np.zeros((WINDOW_H, WINDOW_W, 3), dtype=np.uint8)
        try:
            _boot_clock_photo[0] = _bgr_to_tk_image(_splash_underlay_bgr[0])
            _boot_clock_label.configure(image=_boot_clock_photo[0])
            _boot_clock_label.image = _boot_clock_photo[0]  # type: ignore[attr-defined]
        except Exception:
            pass
        # Keep rasterizing the live saver off the UI thread so splash reveal (and the
        # post-splash bridge) show wall-clock time and the current color — not a
        # frame frozen at process start.
        def _prewarm_splash_clock_worker() -> None:
            while not _splash_clock_refresh_stop[0]:
                try:
                    shown = _rasterize_clock_saver_window_bgr()
                    if shown is not None:
                        _splash_clock_ready_bgr[0] = shown
                except Exception:
                    pass
                if bootstrap_done[0] or _splash_clock_refresh_stop[0]:
                    break
                time.sleep(0.25)

        try:
            threading.Thread(
                target=_prewarm_splash_clock_worker,
                name="pigeon-splash-clock-prewarm",
                daemon=True,
            ).start()
        except Exception:
            shown = _rasterize_clock_saver_window_bgr()
            if shown is not None:
                _splash_clock_ready_bgr[0] = shown

        def _splash_frame_keeps_live_clock(ii: int) -> bool:
            if not splash_png_paths:
                return False
            if callable(splash_keep_alpha_for_live_clock):
                return bool(
                    splash_keep_alpha_for_live_clock(ii, reveal_frame=_splash_reveal_i)
                )
            return int(ii) >= int(_splash_reveal_i)

        # Resolve total frame count AND native fps up front. The PNG / built-in paths lock to
        # SPLASH_FPS, but a video drives its own cadence (e.g. 59.94) so the splash plays at
        # authored speed instead of being stretched or sped up by a hardcoded 30 Hz scheduler.
        _splash_fps_effective = float(max(1, SPLASH_FPS))
        if splash_video_path is not None:
            try:
                _probe = cv2.VideoCapture(str(splash_video_path))
                _vc_total = int(_probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                _vc_fps = float(_probe.get(cv2.CAP_PROP_FPS) or 0.0)
                _probe.release()
            except Exception:
                _vc_total = 0
                _vc_fps = 0.0
            splash_total_frames = max(1, _vc_total) if _vc_total > 0 else FALLBACK_SPLASH_FRAME_COUNT
            # Reject obviously-bogus fps values (VideoCapture sometimes returns 0 or 1000 on bad files).
            if 1.0 < _vc_fps < 240.0:
                _splash_fps_effective = _vc_fps
        elif splash_png_paths:
            splash_total_frames = len(splash_png_paths)
            # Drop trailing empty PNG frames (often 1s+ of held "last frame" after the art is gone).
            if callable(splash_effective_frame_count):
                try:
                    _trimmed = int(
                        splash_effective_frame_count(
                            splash_png_paths, reveal_frame=int(SPLASH_CLOCK_REVEAL_FRAME)
                        )
                    )
                    if 1 <= _trimmed < splash_total_frames:
                        sys.stderr.write(
                            f"pigeon: splash trim frames {splash_total_frames} → {_trimmed} "
                            f"(trailing transparent)\n"
                        )
                        sys.stderr.flush()
                        splash_total_frames = _trimmed
                except Exception:
                    pass
        else:
            splash_total_frames = FALLBACK_SPLASH_FRAME_COUNT

        frame_dt = 1.0 / _splash_fps_effective
        frame_ms = max(1, int(round(1000.0 * frame_dt)))

        # Cap by SPLASH_MAX_DURATION_S so a pathological asset can't block startup.
        _max_frames_for_duration = int(float(SPLASH_MAX_DURATION_S) * _splash_fps_effective)
        if _max_frames_for_duration > 0:
            splash_total_frames = min(splash_total_frames, _max_frames_for_duration)

        # Optional software fade-out (0 = none). Scale with source fps when configured.
        _splash_fade_frames = int(
            round(float(SPLASH_FADE_OUT_FRAMES) * _splash_fps_effective / float(max(1, SPLASH_FPS)))
        )
        if _splash_fade_frames < 0:
            _splash_fade_frames = 0
        _splash_fade_zone_start = max(
            0, splash_total_frames - min(_splash_fade_frames, splash_total_frames)
        )
        _splash_reveal_i = int(SPLASH_CLOCK_REVEAL_FRAME)

        # Two parallel caches keyed by frame index:
        #   * _splash_rgb_cache: opaque RGB over black for pre-reveal frames.
        #   * _splash_bgra_cache: keep alpha for reveal frames so they composite over a live clock.
        _splash_rgb_cache: dict[int, np.ndarray] = {}
        _splash_bgra_cache: dict[int, np.ndarray] = {}
        _splash_photo_cache: dict[int, ImageTk.PhotoImage] = {}
        _splash_prebake_done = [False]

        def _splash_photo_from_rgb(rgb: np.ndarray) -> ImageTk.PhotoImage:
            return ImageTk.PhotoImage(image=Image.fromarray(rgb, "RGB"))

        def _splash_prebuild_photos(*, limit: int) -> int:
            """Turn already-decoded RGB frames into Tk images on the UI thread."""
            built = 0
            for ii in range(splash_total_frames):
                if built >= limit:
                    break
                if _splash_frame_keeps_live_clock(ii):
                    continue
                if ii in _splash_photo_cache:
                    continue
                rgb = _splash_rgb_cache.get(ii)
                if rgb is None:
                    continue
                try:
                    _splash_photo_cache[ii] = _splash_photo_from_rgb(rgb)
                except Exception:
                    break
                built += 1
            return built
        _splash_video_cap_holder: list[cv2.VideoCapture | None] = [None]

        def _splash_raw_bgra(ii: int) -> np.ndarray | None:
            """Source-resolution BGRA for frame ``ii`` (PNG / built-in). Video path doesn't use this."""
            if splash_png_paths:
                if 0 <= ii < len(splash_png_paths):
                    return load_splash_bgra(splash_png_paths[ii])
                return None
            return builtin_splash_bgra_frame(
                ii, splash_total_frames, width=UI_TARGET_W, height=UI_TARGET_H
            )

        def _splash_bgra_over_bgr_to_rgb(bgra: np.ndarray, under_bgr: np.ndarray) -> np.ndarray:
            a = bgra[:, :, 3].astype(np.uint16)
            inv = 255 - a
            blended = (
                bgra[:, :, :3].astype(np.uint16) * a[:, :, None]
                + under_bgr.astype(np.uint16) * inv[:, :, None]
                + 127
            ) // 255
            rgb = cv2.cvtColor(blended.astype(np.uint8), cv2.COLOR_BGR2RGB)
            return np.ascontiguousarray(rgb)

        def _splash_store_prebaked(ii: int, bgra_window: np.ndarray) -> None:
            """Store a window-sized frame. Pre-reveal → RGB over black; reveal PNGs keep BGRA."""
            if _splash_frame_keeps_live_clock(ii):
                _splash_bgra_cache[ii] = bgra_window
                return
            if ii >= _splash_fade_zone_start and _splash_fade_frames > 0:
                _splash_bgra_cache[ii] = bgra_window
                return
            try:
                _splash_rgb_cache[ii] = flatten_bgra_over_bg_to_rgb(bgra_window, _splash_bg_bgr)
            except Exception:
                _splash_bgra_cache[ii] = bgra_window

        def _splash_prebake_reveal_bgra() -> None:
            """Warm BGRA for reveal frames; never flatten them over a frozen clock."""
            try:
                for ii in range(_splash_reveal_i, splash_total_frames):
                    _splash_rgb_cache.pop(ii, None)
                    _splash_photo_cache.pop(ii, None)
                    if ii in _splash_bgra_cache:
                        continue
                    fr = _splash_raw_bgra(ii)
                    if fr is None:
                        continue
                    _splash_bgra_cache[ii] = _bgra_to_display_window(fr)
            except Exception:
                pass

        def _splash_prebake_worker_pngs() -> None:
            """Decode PNGs off the UI thread; flatten early frames over black for a fast blit path."""
            try:
                for ii in range(splash_total_frames):
                    if ii in _splash_rgb_cache or ii in _splash_bgra_cache:
                        continue
                    fr = _splash_raw_bgra(ii)
                    if fr is None:
                        continue
                    fr = _bgra_to_display_window(fr)
                    _splash_store_prebaked(ii, fr)
                _splash_prebake_reveal_bgra()
            except Exception:
                pass
            finally:
                _splash_prebake_done[0] = True

        def _splash_prebake_worker_video() -> None:
            """Sequentially decode the splash video into the fast-path caches.

            Sequential reads on ``VideoCapture`` are hardware-accelerated on macOS
            (AVFoundation/VideoToolbox) and far cheaper than 100+ PNG decodes + resizes.
            """
            cap_v: cv2.VideoCapture | None = None
            try:
                cap_v = cv2.VideoCapture(str(splash_video_path))
                _splash_video_cap_holder[0] = cap_v
                if not cap_v.isOpened():
                    return
                for ii in range(splash_total_frames):
                    ok, bgr = cap_v.read()
                    if not ok or bgr is None:
                        break
                    if bgr.shape[1] != UI_TARGET_W or bgr.shape[0] != UI_TARGET_H:
                        _sw, _sh = int(bgr.shape[1]), int(bgr.shape[0])
                        bgr = cv2.resize(
                            bgr,
                            (UI_TARGET_W, UI_TARGET_H),
                            interpolation=cv_resize_interp(_sw, _sh, UI_TARGET_W, UI_TARGET_H),
                        )
                    bgra_fit = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
                    bgra_fit[:, :, 3] = 255
                    bgra_win = _bgra_to_display_window(bgra_fit)
                    bgr = bgra_win[:, :, :3]
                    if ii < _splash_fade_zone_start:
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        _splash_rgb_cache[ii] = np.ascontiguousarray(rgb)
                    else:
                        # Fade-tail wants dynamic alpha, so synthesize opaque BGRA and let the
                        # tick multiply the alpha channel each frame.
                        h, w = bgr.shape[:2]
                        bgra = np.empty((h, w, 4), dtype=np.uint8)
                        bgra[:, :, :3] = bgr
                        bgra[:, :, 3] = 255
                        _splash_bgra_cache[ii] = bgra
            except Exception:
                pass
            finally:
                try:
                    if cap_v is not None:
                        cap_v.release()
                except Exception:
                    pass
                _splash_video_cap_holder[0] = None
                _splash_prebake_done[0] = True

        # Kick off the prebake thread immediately so frames are warm before ``splash_tick``
        # starts pulling from the cache post-``after_idle``.
        try:
            _worker = _splash_prebake_worker_video if splash_video_path is not None else _splash_prebake_worker_pngs
            _splash_prebake_thread = threading.Thread(
                target=_worker, name="pigeon-splash-prebake", daemon=True
            )
            _splash_prebake_thread.start()
        except Exception:
            # Fall back to on-demand decode inside ``splash_tick``.
            _splash_prebake_done[0] = True

        def _splash_fallback_frame_sync(ii: int) -> np.ndarray | None:
            """On-demand decode if the worker hasn't populated this index yet (built-in only)."""
            if splash_video_path is not None:
                # We can't safely random-seek while the worker owns the VideoCapture.
                return None
            fr = _splash_raw_bgra(ii)
            if fr is None:
                return None
            fr = _bgra_to_display_window(fr)
            _splash_store_prebaked(ii, fr)
            return fr

        def _splash_composite_bgra_to_photo(bgra_hit: np.ndarray | None, fade_mul: float) -> None:
            """Composite splash BGRA over underlay into an opaque RGB ``splash_photo``.

            Before frame 90 the underlay is black; from frame 90 it is the live clock saver.
            Always bake to RGB so the splash layer fully covers content_host.
            """
            if bgra_hit is None:
                return
            bgra_out = (
                apply_splash_global_alpha(bgra_hit, fade_mul)
                if fade_mul < 0.999 and apply_splash_global_alpha is not None
                else bgra_hit
            )
            under = _splash_underlay_bgr[0]
            if under is None or under.ndim != 3 or under.shape[:2] != bgra_out.shape[:2]:
                rgb = flatten_bgra_over_bg_to_rgb(bgra_out, _splash_bg_bgr)
            else:
                rgb = _splash_bgra_over_bgr_to_rgb(bgra_out, under)
            splash_photo[0] = ImageTk.PhotoImage(image=Image.fromarray(rgb, "RGB"))

        def splash_tick() -> None:
            try:
                if not splash_label.winfo_exists():
                    return
            except tk.TclError:
                return
            ov_top = startup_ph[0]
            if ov_top is not None:
                try:
                    # Keep splash strictly above content_host (clock lives underneath).
                    ov_top.lift(content_host)
                except tk.TclError:
                    try:
                        ov_top.lift()
                    except tk.TclError:
                        pass
            ntot = splash_total_frames
            now = time.monotonic()
            if splash_t0[0] is None:
                if _splash_wait_deadline[0] is None:
                    _splash_wait_deadline[0] = now + 3.6
                # Hold the clock until the reveal neighborhood is warm so the
                # Pi does not hitch or jump when PNG alpha starts punching through.
                lead_need = min(ntot, max(48, int(_splash_reveal_i) + 8))
                lead = 0
                for k in range(lead_need):
                    if k in _splash_rgb_cache or k in _splash_bgra_cache:
                        lead += 1
                ready0 = 0 in _splash_rgb_cache or 0 in _splash_bgra_cache
                if ready0 and splash_photo[0] is None:
                    ph0 = _splash_photo_cache.get(0)
                    if ph0 is None:
                        rgb0 = _splash_rgb_cache.get(0)
                        if rgb0 is not None:
                            try:
                                ph0 = _splash_photo_from_rgb(rgb0)
                                _splash_photo_cache[0] = ph0
                            except Exception:
                                ph0 = None
                    if ph0 is not None:
                        splash_photo[0] = ph0
                        try:
                            splash_label.configure(image=ph0)
                        except Exception:
                            pass
                _splash_prebuild_photos(limit=6)
                if (
                    lead < lead_need
                    and not _splash_prebake_done[0]
                    and now < float(_splash_wait_deadline[0])
                ):
                    root.after(8, splash_tick)
                    return
                splash_t0[0] = now
                try:
                    sys.stderr.write(
                        f"pigeon: splash play start frames={ntot} lead={lead}/{lead_need} "
                        f"photos={len(_splash_photo_cache)} "
                        f"+{now - _app_startup_mono:.3f}s\n"
                    )
                    sys.stderr.flush()
                except Exception:
                    pass
            t0 = float(splash_t0[0])
            if float(SPLASH_MAX_DURATION_S) > 0 and now - t0 > float(SPLASH_MAX_DURATION_S):
                splash_idx[0] = ntot
            # Strict order. A held frame looks better than a skip on the Pi.
            i = min(int(splash_idx[0]), ntot)
            if i >= ntot:
                splash_anim_done[0] = True
                try:
                    sys.stderr.write(
                        f"pigeon: splash sequence done +{time.monotonic() - _app_startup_mono:.3f}s "
                        f"frames={ntot}\n"
                    )
                    sys.stderr.flush()
                except Exception:
                    pass
                _try_remove_splash_overlay()
                return

            live_clock_underlay = _splash_frame_keeps_live_clock(i)
            rgb_hit = None if live_clock_underlay else _splash_rgb_cache.get(i)
            bgra_hit = _splash_bgra_cache.get(i) if rgb_hit is None else None
            if live_clock_underlay:
                bgra_hit = _splash_bgra_cache.get(i)

            # Worker hasn't reached this index yet: try a short spin before giving up.
            if rgb_hit is None and bgra_hit is None:
                _splash_fallback_frame_sync(i)
                if live_clock_underlay:
                    bgra_hit = _splash_bgra_cache.get(i)
                else:
                    rgb_hit = _splash_rgb_cache.get(i)
                    bgra_hit = _splash_bgra_cache.get(i) if rgb_hit is None else None
                if rgb_hit is None and bgra_hit is None:
                    # Wait for the prebake worker instead of jumping — skips look choppy.
                    root.after(8, splash_tick)
                    return

            # Frame 90+: live clock under the splash PNG alpha. Before that: black only.
            if i >= _splash_reveal_i:
                first_reveal = not _splash_reveal_clock[0]
                _reveal_clock_under_splash(refresh=False)
                if first_reveal:
                    try:
                        import threading

                        threading.Thread(
                            target=_splash_prebake_reveal_bgra,
                            name="pigeon-splash-reveal-bake",
                            daemon=True,
                        ).start()
                    except Exception:
                        _splash_prebake_reveal_bgra()
                    try:
                        sys.stderr.write(
                            f"pigeon: splash clock reveal frame={i} "
                            f"+{time.monotonic() - _app_startup_mono:.3f}s\n"
                        )
                        sys.stderr.flush()
                    except Exception:
                        pass

            splash_idx[0] = i + 1

            try:
                ph = None if live_clock_underlay else _splash_photo_cache.get(i)
                if ph is None and rgb_hit is not None and not live_clock_underlay:
                    ph = _splash_photo_from_rgb(rgb_hit)
                    _splash_photo_cache[i] = ph
                if ph is not None:
                    splash_photo[0] = ph
                else:
                    fade_mul = (
                        splash_end_fade_factor(i, ntot, min(_splash_fade_frames, ntot))
                        if _splash_fade_frames > 0 and splash_end_fade_factor is not None
                        else 1.0
                    )
                    _splash_composite_bgra_to_photo(bgra_hit, float(fade_mul))
            except Exception:
                # Best-effort RGB fallback so a single bad frame doesn't abort the splash.
                try:
                    fb = rgb_hit if rgb_hit is not None else flatten_bgra_over_bg_to_rgb(
                        bgra_hit if bgra_hit is not None else np.zeros((WINDOW_H, WINDOW_W, 4), np.uint8),
                        _splash_bg_bgr,
                    )
                    splash_photo[0] = ImageTk.PhotoImage(image=Image.fromarray(fb, "RGB"))
                except Exception:
                    pass
            splash_label.configure(image=splash_photo[0])
            # Warm one upcoming frame only — extra encodes on this tick cause hitching.
            _splash_prebuild_photos(limit=1)

            # Hold a late frame rather than jumping; catch up on the next tick.
            now2 = time.monotonic()
            next_i = int(splash_idx[0])
            target = t0 + float(next_i) * frame_dt
            delay_ms = int(round((target - now2) * 1000.0))
            if delay_ms < 1:
                delay_ms = 1
            elif delay_ms > frame_ms * 3:
                delay_ms = frame_ms
            root.after(delay_ms, splash_tick)

    else:
        loading = tk.Label(
            content_host,
            text="Starting Pigeon…\n\n"
            "Tab / Shift+Tab / F9 toggle settings ↔ off. "
            "Key 5 shows grid overlay (press 5 again to toggle detail lines). "
            "Return opens the command bar in settings or grid overlay (5). "
            "Esc closes the bar or quits. F10 / double-click toggles the display. "
            "Space = activate in settings; else play/pause on the selected Player "
            "(Apple TV / Roku) when set; else TMDb backdrop + logo when loaded; else landing brightness pulse.",
            justify="center",
            fg="#ddd",
            bg="#111",
            cursor="none",
            wraplength=WINDOW_W - 40,
        )
        loading.pack(expand=True, fill="both")
        startup_ph[0] = loading

    root.update_idletasks()
    root.update()
    if _PIGEON_EXT and startup_ph[0] is not None:
        try:
            startup_ph[0].lift(content_host)
        except tk.TclError:
            try:
                startup_ph[0].lift()
            except tk.TclError:
                pass

    # Keep idle/paused composites intentionally slower to reduce Tk PhotoImage upload pressure.
    paused_interval_ms = max(67, PAUSED_COMPOSITE_MS)

    def bootstrap() -> None:

        # State created up front (hoisted; side-effect-free initialisers).
        apple_tv_auto_state: dict[str, object] = {
            "running": False,
            "content_key": None,
            "tmdb_key": None,
            "query": None,
            "prefer": "auto",
            "tmdb_fetch_in_flight": False,
            # Latest requested fetch while a worker is already running (drained in finish_tmdb).
            "pending_tmdb": None,
            "last_metadata": None,
            # Last TMDb worker actually started (see spawn_tmdb_poster_fetch); for view 4 debug.
            "last_tmdb_fetch_input": None,
            "last_tmdb_fetch_refined": None,
            "last_tmdb_fetch_prefer": None,
            # Music album art from pyatv ``metadata.artwork()`` (BGRA + track key).
            "music_artwork_bgra": None,
            "music_artwork_key": None,
            # YouTube (and other 16×9) thumbs from pyatv ``metadata.artwork()``.
            "video_artwork_bgra": None,
            "video_artwork_key": None,
            "youtube_thumb_in_flight": False,
            "youtube_thumb_key": None,
            # After no-match / exhausted error-flag retries: stop empty-display poll respawn
            # and show "?" in the circles 2×3 poster slot.
            "tmdb_missing_art": False,
            "tmdb_exhausted_identity": None,
            # One HDMI frame check (clock-saver fingerprint) at a time.
            "hdmi_check_in_flight": False,
            # Last human-readable title decision (also mirrored on last_metadata).
            "last_title_decision": None,
        }
        apple_tv_playback_clock: dict[str, object] = {
            "has_sync": False,
            "sync_mono": 0.0,
            "sync_position": 0.0,
            "live_mode": False,
            "playing": False,
            "latched_total": None,
            "latched_content_key": None,
            "last_reported_total": None,
            # Steady on-screen TRT: integer shown seconds (slewed), not raw extrapolation.
            "display_played_sec": None,
            "trt_next_fire_mono": None,
        }
        apple_tv_dashboard_track: dict[str, object] = {"last_poll_ok": None, "consecutive_fail": 0}
        skip_cache: list[tuple[object, ...] | None] = [None]
        dev_phase = [DevPhase.OFF]
        # Manual [2] force: True = show saver until toggled off (ignores idle timers).
        clock_saver_force_on: list[bool] = [False]
        active_tmdb_title_key: list[str | None] = [None]
        active_tmdb_display_title: list[str | None] = [None]
        streaming_badge_state: dict[str, object] = {
            "show": False,
            "filename": "",
            "label": "",
        }
        _startup_splash_complete: list[bool] = [False]
        tmdb_logo_patch_bgra: list[np.ndarray | None] = [None]

        cap[0] = None

        if not _PIGEON_EXT:
            _w0 = startup_ph[0]
            if _w0 is not None:
                try:
                    _w0.destroy()
                except tk.TclError:
                    pass
                startup_ph[0] = None
        else:
            tk.Widget.pack = _pack_patched  # type: ignore[method-assign]
            tk.Widget.grid = _grid_patched  # type: ignore[method-assign]
            tk.Widget.place = _place_patched  # type: ignore[method-assign]

        try:
            mig = consolidate_legacy_pigeondata_media_folders()
            for line in mig:
                sys.stderr.write(f"pigeon: media folders: {line}\n")
            if mig:
                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"pigeon: media folder consolidation: {e}\n")
            sys.stderr.flush()

        # Full-size video area (always WINDOW_H) so scene scale matches non-overlay mode.
        # Paint the clock onto the new label BEFORE destroying the splash bridge — otherwise
        # the screen flashes black between bridge teardown and the first composite.
        video_area = tk.Frame(content_host, bg="#000", cursor="none")
        label = tk.Label(video_area, bd=0, highlightthickness=0, takefocus=True, bg="#000", cursor="none")
        _startup_label_black_photo: list[ImageTk.PhotoImage | None] = [None]
        _early_clock_underlay_photo: list[ImageTk.PhotoImage | None] = [None]
        ready_clock = _splash_clock_ready_bgr[0]
        if ready_clock is not None:
            _splash_underlay_bgr[0] = ready_clock
            _apply_clock_to_bridge_label(ready_clock)
        _handoff_clock = _boot_clock_photo[0]
        if _handoff_clock is None and _splash_underlay_bgr[0] is not None:
            try:
                _handoff_clock = _bgr_to_tk_image(_splash_underlay_bgr[0])
                _boot_clock_photo[0] = _handoff_clock
            except Exception:
                _handoff_clock = None
        if _handoff_clock is not None:
            try:
                label.configure(image=_handoff_clock)
                label.image = _handoff_clock
                _early_clock_underlay_photo[0] = _handoff_clock
            except Exception:
                _handoff_clock = None
        if _handoff_clock is None:
            try:
                _bb = np.zeros((WINDOW_H, WINDOW_W, 3), dtype=np.uint8)
                _startup_label_black_photo[0] = _bgr_to_tk_image(_bb)
                label.configure(image=_startup_label_black_photo[0])
                label.image = _startup_label_black_photo[0]
            except Exception:
                pass
        video_area.pack(fill=tk.BOTH, expand=True)
        label.pack(fill=tk.BOTH, expand=True)
        try:
            root.update_idletasks()
        except tk.TclError:
            pass
        # Bridge can go away only after the real label already shows the clock.
        try:
            if _boot_clock_host.winfo_exists():
                _boot_clock_host.destroy()
        except tk.TclError:
            pass

        _early_splash_clock_underlay = _bind_deps(
            _core_startup._early_splash_clock_underlay,
            _bgr_to_tk_image=_bgr_to_tk_image,
            _early_clock_underlay_photo=_early_clock_underlay_photo,
            _reveal_clock_under_splash=_reveal_clock_under_splash,
            _splash_reveal_clock=_splash_reveal_clock,
            _splash_underlay_bgr=_splash_underlay_bgr,
            label=label,
        )

        # Keep label in sync once splash has reached the reveal frame.
        _splash_on_reveal_paint[0] = _early_splash_clock_underlay
        if _splash_reveal_clock[0]:
            try:
                _early_splash_clock_underlay()
            except Exception:
                pass

        # Deprecated Tk settings form (never packed). Runtime state helpers below still
        # attach holders here; UI is settings_main SVG only (DevPhase.MAIN_SETTINGS).
        settings_frame = tk.Frame(video_area, bg="#111")
        settings_scroll_outer = tk.Frame(settings_frame, bg="#111")
        settings_scroll_outer.pack(fill=tk.BOTH, expand=True)
        settings_canvas = tk.Canvas(
            settings_scroll_outer,
            bg="#111",
            highlightthickness=0,
            bd=0,
        )
        settings_scrollbar = tk.Scrollbar(
            settings_scroll_outer,
            orient=tk.VERTICAL,
            command=settings_canvas.yview,
            bg="#2a2a2e",
            troughcolor="#111",
            activebackground="#3a3a40",
            highlightthickness=0,
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        settings_inner = tk.Frame(settings_canvas, bg="#111")
        _settings_inner_win = settings_canvas.create_window((16, 12), window=settings_inner, anchor=tk.NW)

        _tk_font_families = frozenset(tkfont.families(root))
        _settings_sans_face = "Helvetica"
        for _cand in (
            "SharpSans",
            "Sharp Sans",
            "SharpSans-Medium",
            "Sharp Sans Medium",
            "SharpSansMedium",
            "Sharp Sans No2",
            "Sharp Sans No1",
        ):
            if _cand in _tk_font_families:
                _settings_sans_face = _cand
                break
        else:
            if "Helvetica Neue" in _tk_font_families:
                _settings_sans_face = "Helvetica Neue"
        _S = _settings_sans_face
        S_FONT_PAGE = (_S, 18, "bold")
        S_FONT_SEC = (_S, 12, "bold")
        S_FONT_BODY = (_S, 10)
        S_FONT_STATUS = (_S, 11)
        S_FONT_BTN = (_S, 11)
        S_FONT_LIST = (_S, 12)
        S_FONT_SMALL = (_S, 9)
        S_FONT_MICRO = (_S, 9)
        S_FONT_CAP_BOLD = (_S, 9, "bold")

        tk.Label(
            settings_inner,
            text="Pigeon Settings",
            fg="#f5f5f5",
            bg="#111",
            font=S_FONT_PAGE,
        ).pack(anchor=tk.W, pady=(0, 4))

        # Inner <Configure> fires often while scrolling an embedded window; only refresh scrollregion
        # when the inner frame actually changes size to avoid canvas flicker / jumpy redraws.
        _settings_inner_scroll_size: list[int] = [0, 0]

        _settings_update_scrollregion = _bind_deps(
            _core_settings_ui._settings_update_scrollregion,
            _settings_inner_scroll_size=_settings_inner_scroll_size,
            settings_canvas=settings_canvas,
            settings_inner=settings_inner,
        )

        _settings_on_canvas_configure = _bind_deps(
            _core_settings_ui._settings_on_canvas_configure,
            _settings_inner_win=_settings_inner_win,
            _settings_update_scrollregion=_settings_update_scrollregion,
            root=root,
            settings_canvas=settings_canvas,
        )

        settings_inner.bind("<Configure>", _settings_update_scrollregion)
        settings_canvas.bind("<Configure>", _settings_on_canvas_configure)

        settings_wheel_all_bound = [False]
        last_pigeon_user_activity_mono = [time.monotonic()]
        # Shared with metadata-idle clock saver (defined early so control bumps can dismiss it).
        last_metadata_activity_mono = [time.monotonic()]
        # Until playback is seen (or the user uses a control), stay on the clock saver after splash.
        _boot_clock_saver_until_playback = [True]

        _bump_pigeon_user_activity = _bind_deps(
            _core_saver_state._bump_pigeon_user_activity,
            _boot_clock_saver_until_playback=_boot_clock_saver_until_playback,
            last_metadata_activity_mono=last_metadata_activity_mono,
            last_pigeon_user_activity_mono=last_pigeon_user_activity_mono,
        )

        def _maybe_exit_settings_menus_on_idle(now_mono: float | None = None) -> bool:
            """Close settings_main after ``SETTINGS_MENU_IDLE_EXIT_S`` without input.

            Returns True when menus were closed (compose should show now-playing or
            clock saver via the normal OFF-phase path).
            """
            if dev_phase[0] != DevPhase.MAIN_SETTINGS:
                return False
            now_i = float(now_mono if now_mono is not None else time.monotonic())
            if (now_i - float(last_pigeon_user_activity_mono[0])) < float(
                SETTINGS_MENU_IDLE_EXIT_S
            ):
                return False
            if main_settings_widget is not None:
                try:
                    if not bool(getattr(main_settings_widget.state, "exit_enabled", True)):
                        return False
                except Exception:
                    pass
            if main_settings_widget is not None:
                try:
                    st_ms = main_settings_widget.state
                    if st_ms.keyboard_open:
                        st_ms.close_keyboard(commit=False)
                    st_ms.exit_pigeon_settings()
                    main_settings_widget.invalidate()
                except Exception:
                    pass
            dev_phase[0] = DevPhase.OFF
            skip_cache[0] = None
            try:
                sync_developer_chrome()
            except Exception:
                pass
            return True

        def _sync_preferences_now_playing_progress() -> None:
            """Feed live NP content into prefs / widgets; idle keeps SVG demos."""
            if main_settings_widget is None:
                return
            st_ms = main_settings_widget.state
            if not st_ms.show_preferences and not st_ms.show_widgets:
                return

            def _clear_prefs_live() -> None:
                st_ms.preferences_live_content = False
                st_ms.preferences_np_progress = None
                st_ms.preferences_poster_bgra = None
                st_ms.preferences_volume = None
                st_ms.preferences_volume_fraction = None
                st_ms.preferences_incoming = None
                st_ms.preferences_config = None
                st_ms.preferences_cast = None
                st_ms.preferences_elapsed_text = None
                st_ms.preferences_remaining_text = None
                st_ms.preferences_service_name = None
                st_ms.preferences_content_mode = None
                st_ms.preferences_song_title = None
                st_ms.preferences_album_title = None
                st_ms.preferences_artist_title = None
                st_ms.preferences_tt_bgra = None

            try:
                prog = _playback_progress_fraction_for_bar()
            except Exception:
                prog = None
            clk = apple_tv_playback_clock
            has_playback = bool(clk.get("has_sync") or clk.get("live_mode"))
            poster = None
            try:
                poster = _circles_poster_bgra()
            except Exception:
                poster = None
            live = bool(
                has_playback
                or prog is not None
                or (poster is not None and getattr(poster, "size", 0) > 0)
                or bool(str(active_tmdb_title_key[0] or "").strip())
            )
            if not live:
                _clear_prefs_live()
                return

            st_ms.preferences_live_content = True
            st_ms.preferences_np_progress = prog
            st_ms.preferences_poster_bgra = poster
            try:
                inc, cfg, vol = _resolve_receiver_lines_for_now_playing()
            except Exception:
                inc, cfg, vol = "", "", ""
            st_ms.preferences_incoming = inc
            st_ms.preferences_config = cfg
            st_ms.preferences_volume = vol
            try:
                from pigeon.widgets.playback_overlay import volume_fraction_from_display_line

                st_ms.preferences_volume_fraction = float(
                    volume_fraction_from_display_line(vol)
                )
            except Exception:
                st_ms.preferences_volume_fraction = 0.0

            remaining_text = ""
            played_text = ""
            if clk.get("live_mode"):
                remaining_text = "LIVE"
                played_text = "LIVE"
            else:
                try:
                    pair = _playback_extrapolated_pair()
                except Exception:
                    pair = None
                if pair is not None:
                    played_text = _format_hmmss(int(pair[0]))
                    remaining_text = _format_hmmss(int(pair[1]))
            st_ms.preferences_elapsed_text = played_text
            st_ms.preferences_remaining_text = remaining_text

            sb = streaming_badge_state
            svc = str(sb.get("label") or "").strip()
            if not svc:
                lm_svc = apple_tv_auto_state.get("last_metadata")
                if isinstance(lm_svc, dict):
                    svc = str(lm_svc.get("app_name") or "").strip()
            st_ms.preferences_service_name = svc

            is_music = bool(_vv_is_music())
            st_ms.preferences_content_mode = "music" if is_music else "video"
            if is_music:
                lm_music = apple_tv_auto_state.get("last_metadata")
                song_t = album_t = artist_t = ""
                if isinstance(lm_music, dict):
                    song_t = str(lm_music.get("title") or "").strip()
                    album_t = str(lm_music.get("album") or "").strip()
                    artist_t = str(lm_music.get("artist") or "").strip()
                    if not song_t and album_t:
                        song_t, album_t = album_t, ""
                st_ms.preferences_song_title = song_t
                st_ms.preferences_album_title = album_t
                st_ms.preferences_artist_title = artist_t
                st_ms.preferences_cast = ()
                st_ms.preferences_tt_bgra = poster
            else:
                st_ms.preferences_song_title = str(
                    active_tmdb_display_title[0] or ""
                ).strip()
                st_ms.preferences_album_title = ""
                st_ms.preferences_artist_title = ""
                cast_rows: list[tuple[str, str]] = []
                try:
                    from pigeon.tmdb_poster import get_cached_tmdb_cast

                    tk = str(active_tmdb_title_key[0] or "").strip()
                    if tk:
                        cast_rows = list(get_cached_tmdb_cast(tk) or [])
                except Exception:
                    cast_rows = []
                st_ms.preferences_cast = tuple(
                    (str(a or ""), str(c or "")) for a, c in cast_rows[:9]
                )
                try:
                    st_ms.preferences_tt_bgra = _active_tmdb_tt_src_bgra()
                except Exception:
                    st_ms.preferences_tt_bgra = None

        _settings_wheel_target_should_ignore = _core_settings_ui._settings_wheel_target_should_ignore

        _settings_is_under_scroll_surface = _bind_deps(
            _core_settings_ui._settings_is_under_scroll_surface,
            settings_scroll_outer=settings_scroll_outer,
        )

        _settings_mousewheel = _bind_deps(
            _core_settings_ui._settings_mousewheel,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _settings_is_under_scroll_surface=_settings_is_under_scroll_surface,
            _settings_wheel_target_should_ignore=_settings_wheel_target_should_ignore,
            root=root,
            settings_canvas=settings_canvas,
            settings_frame=settings_frame,
        )

        _settings_bind_wheel_globals = _bind_deps(
            _core_settings_ui._settings_bind_wheel_globals,
            _settings_mousewheel=_settings_mousewheel,
            root=root,
            settings_wheel_all_bound=settings_wheel_all_bound,
        )

        _settings_unbind_wheel_globals = _bind_deps(
            _core_settings_ui._settings_unbind_wheel_globals,
            root=root,
            settings_wheel_all_bound=settings_wheel_all_bound,
        )

        on_purge_image_media = _bind_deps(_core_settings_ui.on_purge_image_media, root=root)

        location_section = tk.Frame(settings_inner, bg="#111")
        location_section.pack(anchor=tk.W, fill=tk.X, pady=(8, 2))
        location_menu_var = tk.StringVar(value="")
        location_name_var = tk.StringVar(value="")
        location_option_holder: list[tk.OptionMenu | None] = [None]
        location_om_parent = tk.Frame(location_section, bg="#111")
        location_om_parent.pack(anchor=tk.W, fill=tk.X)
        tk.Label(
            location_om_parent,
            text="Current location",
            fg="#ccc",
            bg="#111",
            font=S_FONT_SEC,
        ).pack(side=tk.LEFT)
        location_om_frame = tk.Frame(location_om_parent, bg="#111")
        location_om_frame.pack(side=tk.LEFT, padx=(10, 0))
        delete_location_btn = tk.Button(
            location_om_parent,
            text="\u2715",
            font=("Helvetica", 12, "bold"),
            fg="#c44",
            bg="#111",
            activebackground="#2a1a1a",
            activeforeground="#f66",
            highlightthickness=1,
            highlightbackground="#553333",
            bd=0,
            cursor="none" if _kiosk_on else "hand2",
            padx=8,
            pady=0,
            command=lambda: None,
            state=tk.DISABLED,
        )
        delete_location_btn.pack(side=tk.LEFT, padx=(10, 0), anchor=tk.W)
        location_edit_row = tk.Frame(location_section, bg="#111")
        location_edit_row.pack(anchor=tk.W, fill=tk.X, pady=(6, 0))
        tk.Label(
            location_edit_row,
            text="Location name",
            fg="#888",
            bg="#111",
            font=S_FONT_MICRO,
        ).pack(side=tk.LEFT)
        location_name_entry = tk.Entry(
            location_edit_row,
            textvariable=location_name_var,
            width=28,
            bg="#1a1a1e",
            fg="#e8e8e8",
            insertbackground="#e8e8e8",
            highlightthickness=1,
            highlightbackground="#333",
            font=S_FONT_BODY,
        )
        location_name_entry.pack(side=tk.LEFT, padx=(8, 6))

        _apply_location_rename = _bind_deps(
            _core_settings_ui._apply_location_rename,
            _apply_persisted_location_to_runtime=_late(lambda: _apply_persisted_location_to_runtime, "_apply_persisted_location_to_runtime"),
            _refresh_location_selector=_late(lambda: _refresh_location_selector, "_refresh_location_selector"),
            _start_location_toast=_late(lambda: _start_location_toast, "_start_location_toast"),
            location_name_var=location_name_var,
        )

        rename_name_btn = tk.Button(
            location_edit_row,
            text="Save name",
            command=_apply_location_rename,
            font=S_FONT_BTN,
            padx=10,
            pady=2,
        )
        rename_name_btn.pack(side=tk.LEFT)

        _on_location_name_return = _bind_deps(
            _core_settings_ui._on_location_name_return,
            _apply_location_rename=_apply_location_rename,
        )

        location_name_entry.bind("<Return>", _on_location_name_return)
        tk.Label(
            location_section,
            text="Playback and overlay follow the active location. Rename anytime; home room names are not inferred from device scan — set them here or when adding a custom location in Find device.",
            fg="#666",
            bg="#111",
            font=S_FONT_MICRO,
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))

        apple_tv_section = tk.Frame(settings_inner, bg="#111")
        apple_tv_section.pack(anchor=tk.W, pady=(4, 3))
        streaming_slot_holder: list[dict[str, str] | None] = [None]
        avr_slot_holder: list[dict[str, str] | None] = [None]
        receiver_poll_busy = {"active": False}
        # Recent pyatv scan (non-empty results only); avoids full LAN scan when adding more devices.
        DISCOVERY_CACHE_TTL_S = 600.0
        discovery_scan_cache: dict[str, object] = {"rows": None, "mono_s": 0.0}
        pairing_led_holder: list[tk.Canvas | None] = [None, None]
        pair_led_busy = {"active": False}
        _pair_led_pending_retry = [False]
        current_apple_tv = read_last_apple_tv()
        receiver_http_host = {"host": str(read_last_receiver().get("host") or "").strip()}
        apple_tv_status_var = tk.StringVar()
        streaming_row_led_canvas_holder: list[tk.Canvas | None] = [None]
        receiver_panel_led_holder: list[tk.Canvas | None] = [None]
        paired_ui_leds: dict[str, tk.Canvas | None] = {"remote": None, "airplay": None, "receiver": None}
        paired_observed_led_rows: list[tuple[tk.Canvas, str, dict[str, str]]] = []
        paired_observed_led_last_state: dict[int, bool | None] = {}
        settings_footer_debug_holder: list[tk.Button | None] = [None]
        settings_footer_reset_holder: list[tk.Button | None] = [None]
        match_quality_glance_label_holder: list[tk.Label | None] = [None]
        match_quality_glance_sig: list[str] = [""]
        apple_tv_busy = {"active": False}
        tmdb_retry_rule_idx = [0]
        tmdb_adv_manual_btn_holder: list[tk.Button | None] = [None]
        tmdb_adv_report_btn_holder: list[tk.Button | None] = [None]
        tmdb_adv_log_text_holder: list[tk.Misc | None] = [None]

        _register_tmdb_adv_widgets = _bind_deps(
            _core_settings_ui._register_tmdb_adv_widgets,
            tmdb_adv_log_text_holder=tmdb_adv_log_text_holder,
            tmdb_adv_manual_btn_holder=tmdb_adv_manual_btn_holder,
            tmdb_adv_report_btn_holder=tmdb_adv_report_btn_holder,
        )

        _unregister_tmdb_adv_widgets = _bind_deps(
            _core_settings_ui._unregister_tmdb_adv_widgets,
            tmdb_adv_log_text_holder=tmdb_adv_log_text_holder,
            tmdb_adv_manual_btn_holder=tmdb_adv_manual_btn_holder,
            tmdb_adv_report_btn_holder=tmdb_adv_report_btn_holder,
        )

        _append_tmdb_retry_log_ui = _bind_deps(
            _core_settings_ui._append_tmdb_retry_log_ui,
            tmdb_adv_log_text_holder=tmdb_adv_log_text_holder,
        )


        _has_playback_position = _bind_deps(
            _core_now_playing._has_playback_position,
            _playback_progress_fraction_for_bar=_late(lambda: _playback_progress_fraction_for_bar, "_playback_progress_fraction_for_bar"),
        )

        _metadata_is_netflix_app = _core_now_playing._metadata_is_netflix_app

        _atv_metadata_is_content_idle = _bind_deps(
            _core_now_playing._atv_metadata_is_content_idle,
            metadata_has_playback_title=metadata_has_playback_title,
            resolve_metadata_tmdb_query=resolve_metadata_tmdb_query,
        )

        _apple_tv_is_off = _bind_deps(
            _core_device_control._apple_tv_is_off,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            current_apple_tv=current_apple_tv,
        )

        _show_paused_row_overlay = _bind_deps(
            _core_saver_state._show_paused_row_overlay,
            _apple_tv_is_off=_apple_tv_is_off,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        content_indicator_cv_holder: list[tk.Canvas | None] = [None]
        _LISTBOX_BG = "#1a1a1e"
        _LISTBOX_FG = "#e8e8e8"

        atv_status_row = tk.Frame(apple_tv_section, bg="#111")
        atv_status_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        tk.Label(
            atv_status_row,
            textvariable=apple_tv_status_var,
            fg="#cfcfcf",
            bg="#111",
            font=S_FONT_STATUS,
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        devices_strip = tk.Frame(apple_tv_section, bg="#111")
        devices_strip.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        devices_btn_row = tk.Frame(devices_strip, bg="#111")
        devices_btn_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 4))
        find_device_btn = tk.Button(
            devices_btn_row,
            text="Find device",
            command=lambda: _open_find_device_dialog(),
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        find_device_btn.pack(side=tk.LEFT)
        advanced_matrix_btn = tk.Button(
            devices_btn_row,
            text="Advanced",
            command=lambda: _open_advanced_capability_matrix(),
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        advanced_matrix_btn.pack(side=tk.LEFT, padx=(10, 0))
        update_check_state: dict[str, object] = {
            "update_available": False,
            "remote_version": None,
            "github_branch": None,
            "checking": False,
            "last_check_mono": 0.0,
            "error": None,
            "applying": False,
        }
        _UPDATE_CHECK_INTERVAL_S = 30 * 60

        _match_neighbor_button_style = _core_settings_ui._match_neighbor_button_style

        def _sync_update_button_style() -> None:
            _match_neighbor_button_style(update_btn, ref=find_device_btn)
            if update_check_state.get("update_available"):
                update_btn.configure(
                    text="Updates ●",
                    state=tk.NORMAL,
                )
            else:
                update_btn.configure(
                    text="Updates",
                    state=tk.NORMAL,
                )

        _resolve_install_root_for_update = _core_settings_ui._resolve_install_root_for_update

        def _run_github_apply_worker(*, remote: str = "?", branch: str | None = None) -> None:
            install_root = _resolve_install_root_for_update()
            progress = tk.Toplevel(root)
            progress.title("Updating Pigeon")
            progress.transient(root)
            progress.grab_set()
            status_var = tk.StringVar(value="Downloading from GitHub…")
            tk.Label(progress, textvariable=status_var, padx=16, pady=16).pack()
            update_check_state["applying"] = True
            update_btn.configure(state=tk.DISABLED)

            def worker() -> None:
                try:
                    from pigeon.github_update import apply_github_update

                    apply_branch = branch
                    if apply_branch is None:
                        cached = update_check_state.get("github_branch")
                        if isinstance(cached, str) and cached.strip():
                            apply_branch = cached.strip()
                    result = apply_github_update(install_root, branch=apply_branch)
                except Exception as e:
                    from pigeon.github_update import ApplyUpdateResult

                    result = ApplyUpdateResult(False, str(e))

                def finish_apply() -> None:
                    update_check_state["applying"] = False
                    update_btn.configure(state=tk.NORMAL)
                    if result.ok:
                        status_var.set("Update complete — restarting Pigeon…")
                        update_check_state["update_available"] = False
                        if result.remote_version:
                            update_check_state["remote_version"] = result.remote_version
                        else:
                            update_check_state["remote_version"] = remote
                        _sync_update_button_style()

                        def _restart_and_exit() -> None:
                            try:
                                progress.grab_release()
                                progress.destroy()
                            except tk.TclError:
                                pass
                            try:
                                from pigeon.github_update import restart_pigeon_after_update

                                restart_pigeon_after_update(
                                    install_root, parent_pid=os.getpid()
                                )
                            except Exception:
                                pass
                            try:
                                root.destroy()
                            except tk.TclError:
                                pass
                            os._exit(0)

                        root.after(400, _restart_and_exit)
                        return

                    try:
                        progress.grab_release()
                        progress.destroy()
                    except tk.TclError:
                        pass
                    messagebox.showerror(
                        "Update failed",
                        result.message,
                        parent=root,
                    )

                root.after(0, finish_apply)

            threading.Thread(target=worker, daemon=True).start()

        _linux_on_updates_button = _bind_deps(
            _core_settings_ui._linux_on_updates_button,
            _run_github_apply_worker=_run_github_apply_worker,
            root=root,
            update_check_state=update_check_state,
        )

        _begin_apply_update = _bind_deps(
            _core_settings_ui._begin_apply_update,
            _run_github_apply_worker=_run_github_apply_worker,
            root=root,
        )

        def _on_updates_button() -> None:
            if sys.platform.startswith("linux"):
                _linux_on_updates_button()
                return
            if update_check_state.get("applying") or update_check_state.get("checking"):
                return

            progress = tk.Toplevel(root)
            progress.title("Updates")
            progress.transient(root)
            progress.grab_set()
            status_var = tk.StringVar(value="Checking GitHub for updates…")
            tk.Label(progress, textvariable=status_var, padx=16, pady=16).pack()
            update_check_state["checking"] = True
            update_btn.configure(state=tk.DISABLED)

            def worker() -> None:
                try:
                    from pigeon.update_check import check_for_update

                    result = check_for_update(force=True)
                except Exception as e:
                    from pigeon.update_check import UpdateCheckResult

                    result = UpdateCheckResult(
                        local_version=version_string(),
                        remote_version=None,
                        update_available=False,
                        error=str(e),
                    )

                def finish_check() -> None:
                    update_check_state["checking"] = False
                    _finish_update_check(result)
                    try:
                        progress.grab_release()
                        progress.destroy()
                    except tk.TclError:
                        pass
                    update_btn.configure(state=tk.NORMAL)

                    from pigeon.update_check import UpdateCheckResult

                    if not isinstance(result, UpdateCheckResult):
                        return
                    if result.error:
                        messagebox.showerror(
                            "Updates",
                            f"Could not check GitHub for updates.\n\n"
                            f"Installed: {result.local_version}\n\n"
                            f"{result.error}",
                            parent=root,
                        )
                        return
                    if result.update_available:
                        _begin_apply_update(
                            remote=str(result.remote_version or "?"),
                            branch=result.github_branch,
                        )
                        return
                    remote = result.remote_version
                    if remote:
                        body = (
                            f"You are on the latest version GitHub reports.\n\n"
                            f"Installed: {result.local_version}\n"
                            f"GitHub:    {remote}"
                        )
                    else:
                        body = (
                            f"No update information from GitHub.\n\n"
                            f"Installed: {result.local_version}\n\n"
                            f"If the repo is private, set PIGEON_UPDATE_GITHUB_TOKEN "
                            f"in the environment and try again."
                        )
                    messagebox.showinfo("Updates", body, parent=root)

                root.after(0, finish_check)

            threading.Thread(target=worker, daemon=True).start()

        _finish_update_check = _bind_deps(
            _core_settings_ui._finish_update_check,
            _sync_update_button_style=_sync_update_button_style,
            update_check_state=update_check_state,
        )

        _check_for_updates = _bind_deps(
            _core_settings_ui._check_for_updates,
            _UPDATE_CHECK_INTERVAL_S=_UPDATE_CHECK_INTERVAL_S,
            _finish_update_check=_finish_update_check,
            root=root,
            update_check_state=update_check_state,
        )

        _schedule_periodic_update_check = _bind_deps(
            _core_settings_ui._schedule_periodic_update_check,
            _UPDATE_CHECK_INTERVAL_S=_UPDATE_CHECK_INTERVAL_S,
            _check_for_updates=_check_for_updates,
            _schedule_periodic_update_check=_late(lambda: _schedule_periodic_update_check, "_schedule_periodic_update_check"),
            root=root,
        )

        update_btn = tk.Button(
            devices_btn_row,
            text="Updates",
            command=_on_updates_button,
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        update_btn.pack(side=tk.LEFT, padx=(10, 0))
        _sync_update_button_style()
        root.after(4000, lambda: _check_for_updates(force=True))
        root.after(int(_UPDATE_CHECK_INTERVAL_S * 1000), _schedule_periodic_update_check)
        tk.Label(
            devices_strip,
            text="Choose a device role, pick a device or enter Host/IP, then Confirm to save it to the current location.",
            fg="#666",
            bg="#111",
            font=S_FONT_MICRO,
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        paired_devices_inner = tk.Frame(apple_tv_section, bg="#111")
        paired_devices_inner.pack(anchor=tk.W, fill=tk.X, pady=(0, 8))

        content_section = tk.Frame(apple_tv_section, bg="#111")
        content_section.pack(anchor=tk.W, fill=tk.X, pady=(2, 6))
        content_heading_row = tk.Frame(content_section, bg="#111")
        content_heading_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        tk.Label(
            content_heading_row,
            text="Content",
            fg="#ccc",
            bg="#111",
            font=S_FONT_SEC,
        ).pack(side=tk.LEFT)
        content_indicator_cv = tk.Canvas(
            content_heading_row,
            width=20,
            height=20,
            bg="#111",
            highlightthickness=0,
            bd=0,
        )
        content_indicator_cv.pack(side=tk.LEFT, padx=(10, 6))
        content_indicator_cv_holder[0] = content_indicator_cv
        _paint_boolean_led(content_indicator_cv, False)
        tk.Label(
            content_heading_row,
            text="● green = detected   ● red = not",
            fg="#666",
            bg="#111",
            font=S_FONT_MICRO,
        ).pack(side=tk.LEFT, padx=(6, 0))
        content_buttons_row = tk.Frame(content_section, bg="#111")
        content_buttons_row.pack(anchor=tk.W, pady=(0, 6))
        # Purge is parented here after handlers. TMDb (Manual Fetch, Report Failure, retry log) lives on Advanced.

        # Bottom “info bar” HUD removed; Tab toggles settings_main ↔ off.
        hud_bar = None
        hud = None

        fps_sched = _default_render_fps()
        display_dims = [WINDOW_W, WINDOW_H]
        fit_holder = [SceneFit(target_w=WINDOW_W, target_h=WINDOW_H)]
        try:
            from pigeon.display_par import read_par_mode, resolve_display_par

            _par0, _par_reason0 = resolve_display_par(
                display_w=int(display_dims[0]), display_h=int(display_dims[1])
            )
            sys.stderr.write(
                f"pigeon: display PAR mode={read_par_mode()}  "
                f"effective={_par0:.4f}  ({_par_reason0})  "
                f"[hold P+A+R to toggle auto/off]\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

        if _PIGEON_EXT:
            from pigeon.design import DESIGN_W as _DESIGN_W_L, DESIGN_H as _DESIGN_H_L

            _land_w, _land_h = int(_DESIGN_W_L), int(_DESIGN_H_L)
        else:
            _land_w, _land_h = UI_TARGET_W, UI_TARGET_H
        # No PNG on the landing plate; playback overlay is badge + receiver lines only.
        landing_scene_design_bgr = _build_landing_design_bgr(_land_w, _land_h, None)

        _disp_fit = _bind_deps(_core_stage_render._disp_fit, fit_holder=fit_holder)

        _black_screen_bgr = _bind_deps(
            _core_stage_render._black_screen_bgr,
            display_dims=display_dims,
        )

        frame_interval_ms = [max(1, int(round(1000.0 / fps_sched)))]

        playing = [False]
        display_view_holder: list[DisplayView] = [DisplayView.ONE]
        # View 4 (key 4): 0=Title Info, 1=Source Info, 2=Playback Info — press 4 again to cycle.
        view_four_subview_holder: list[int] = [0]
        # View 5: 0=viewFive_a, 1=viewFive_b, 2=viewFive_c (testing text view).
        view_five_mode_holder: list[int] = [0]
        # View 1 (Shift+1): cycle a -> b -> c layouts (see ``ViewOneLayout``).
        view_one_layout_holder: list[int] = [int(ViewOneLayout.PIGEON_FULL)]
        # Last view-1 a/b/c while view 1 was live — developer GRID and view-5 grid overlay composite this.
        last_view_one_layout_snapshot: list[int] = [int(ViewOneLayout.PIGEON_FULL)]

        _capture_last_view_one_layout_from_live_view = _bind_deps(
            _core_view_one._capture_last_view_one_layout_from_live_view,
            DisplayView=DisplayView,
            display_view_holder=display_view_holder,
            last_view_one_layout_snapshot=last_view_one_layout_snapshot,
            view_one_layout_holder=view_one_layout_holder,
        )

        _view_one_layout_effective = _bind_deps(
            _core_view_one._view_one_layout_effective,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
            last_view_one_layout_snapshot=last_view_one_layout_snapshot,
            view_one_layout_holder=view_one_layout_holder,
        )

        _stage_is_view_one_video_layout = _bind_deps(
            _core_view_one._stage_is_view_one_video_layout,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
        )

        _view_one_is_pigeon_full = _bind_deps(
            _core_view_one._view_one_is_pigeon_full,
            ViewOneLayout=ViewOneLayout,
            _stage_is_view_one_video_layout=_stage_is_view_one_video_layout,
            _view_one_layout_effective=_view_one_layout_effective,
        )

        _view_one_is_pigeon_simple = _bind_deps(
            _core_view_one._view_one_is_pigeon_simple,
            ViewOneLayout=ViewOneLayout,
            _stage_is_view_one_video_layout=_stage_is_view_one_video_layout,
            _view_one_layout_effective=_view_one_layout_effective,
        )

        _view_one_is_pigeon_poster = _bind_deps(
            _core_view_one._view_one_is_pigeon_poster,
            ViewOneLayout=ViewOneLayout,
            _stage_is_view_one_video_layout=_stage_is_view_one_video_layout,
            _view_one_layout_effective=_view_one_layout_effective,
        )
        last_frame: list[np.ndarray | None] = [landing_scene_design_bgr]
        brightness_current = [LANDING_DISPLAY_BRIGHTNESS]
        brightness_from = [LANDING_DISPLAY_BRIGHTNESS]
        brightness_target = [LANDING_DISPLAY_BRIGHTNESS]
        brightness_t0 = [time.monotonic()]
        brightness_duration_s = [3.0]
        brightness_duration_up_s = 1.0
        brightness_duration_down_s = 1.0

        last_atv_interaction_mono = [0.0]
        last_timecode_motion_mono = [0.0]
        # ``last_metadata_activity_mono`` is initialized with pigeon user-activity state above.
        last_clock_saver_significant_device_mono = [time.monotonic()]
        # Monotonic start of the current paused-with-content hold; 0 while not paused.
        _paused_row_since_mono = [0.0]
        _paused_row_last_hold_mono = [0.0]
        _cs_sig_init = [False]
        _cs_sig_ck: list[str | None] = [None]
        _cs_sig_ds = [""]
        _cs_sig_vol = [""]
        _cs_sig_fp = [""]
        _atv_ix_sig_ds = [""]
        _atv_ix_sig_ck: list[str | None] = [None]
        _atv_ix_pos: list[float | None] = [None]
        _atv_ix_pos_mono = [0.0]
        _atv_ix_extrap_playing = [False]
        _atv_ix_prev_idle = [True]

        _atv_idle_monochrome_active = _bind_deps(
            _core_saver_state._atv_idle_monochrome_active,
            THEATER_IDLE_DIM_AFTER_S=THEATER_IDLE_DIM_AFTER_S,
            apple_tv_playback_clock=apple_tv_playback_clock,
            current_apple_tv=current_apple_tv,
            last_atv_interaction_mono=last_atv_interaction_mono,
            last_pigeon_user_activity_mono=last_pigeon_user_activity_mono,
        )

        _vol_norm_for_clock_saver = _core_saver_state._vol_norm_for_clock_saver

        _bump_clock_saver_significant_device = _bind_deps(
            _core_saver_state._bump_clock_saver_significant_device,
            last_clock_saver_significant_device_mono=last_clock_saver_significant_device_mono,
        )

        _note_metadata_activity = _bind_deps(
            _core_saver_state._note_metadata_activity,
            last_metadata_activity_mono=last_metadata_activity_mono,
        )

        _clear_reported_position_stall_stamp = _bind_deps(
            _core_saver_state._clear_reported_position_stall_stamp,
            last_metadata_activity_mono=last_metadata_activity_mono,
        )

        _coarse_device_state_for_saver = _core_saver_state._coarse_device_state_for_saver

        _metadata_activity_fingerprint = _bind_deps(
            _core_now_playing._metadata_activity_fingerprint,
            _coarse_device_state_for_saver=_coarse_device_state_for_saver,
            _content_key_from_metadata=_late(lambda: _content_key_from_metadata, "_content_key_from_metadata"),
        )

        _bump_clock_saver_significant_device_from_metadata = _bind_deps(
            _core_saver_state._bump_clock_saver_significant_device_from_metadata,
            _bump_clock_saver_significant_device=_bump_clock_saver_significant_device,
            _coarse_device_state_for_saver=_coarse_device_state_for_saver,
            _content_key_from_metadata=_late(lambda: _content_key_from_metadata, "_content_key_from_metadata"),
            _cs_sig_ck=_cs_sig_ck,
            _cs_sig_ds=_cs_sig_ds,
            _cs_sig_fp=_cs_sig_fp,
            _cs_sig_init=_cs_sig_init,
            _cs_sig_vol=_cs_sig_vol,
            _metadata_activity_fingerprint=_metadata_activity_fingerprint,
            _note_metadata_activity=_note_metadata_activity,
            _vol_norm_for_clock_saver=_vol_norm_for_clock_saver,
        )
            # Receiver / player volume must not dismiss the idle clock saver.

        _reset_clock_saver_device_signal_baseline = _bind_deps(
            _core_saver_state._reset_clock_saver_device_signal_baseline,
            _bump_clock_saver_significant_device=_bump_clock_saver_significant_device,
            _cs_sig_ck=_cs_sig_ck,
            _cs_sig_ds=_cs_sig_ds,
            _cs_sig_fp=_cs_sig_fp,
            _cs_sig_init=_cs_sig_init,
            _cs_sig_vol=_cs_sig_vol,
            _note_metadata_activity=_note_metadata_activity,
        )

        _apply_position_stall_grace_to_clock_saver = _bind_deps(
            _core_saver_state._apply_position_stall_grace_to_clock_saver,
            CLOCK_SAVER_POSITION_STALL_GRACE_S=CLOCK_SAVER_POSITION_STALL_GRACE_S,
            last_clock_saver_significant_device_mono=last_clock_saver_significant_device_mono,
            last_timecode_motion_mono=last_timecode_motion_mono,
        )

        _cs_meta_idle_log_mono = [0.0]
        _cs_meta_idle_was_active = [False]

        _something_playing_now = _bind_deps(
            _core_saver_state._something_playing_now,
            _apple_tv_is_off=_apple_tv_is_off,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _tmdb_info_current_and_available = _bind_deps(
            _core_tmdb_flow._tmdb_info_current_and_available,
            _tmdb_spawn_identity=_late(lambda: _tmdb_spawn_identity, "_tmdb_spawn_identity"),
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _metadata_drives_clock_saver = _bind_deps(
            _core_saver_state._metadata_drives_clock_saver,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _clock_saver_idle_need = _core_saver_state._clock_saver_idle_need

        _clock_saver_user_enabled = _core_saver_state._clock_saver_user_enabled

        _player_metadata_class = _bind_deps(
            _core_now_playing._player_metadata_class,
            _playback_extrapolated_pair=_late(lambda: _playback_extrapolated_pair, "_playback_extrapolated_pair"),
            _show_paused_row_overlay=_show_paused_row_overlay,
            _something_playing_now=_something_playing_now,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _pausesaver_is_holding = _bind_deps(
            _core_saver_state._pausesaver_is_holding,
            _player_metadata_class=_player_metadata_class,
            _show_paused_row_overlay=_show_paused_row_overlay,
        )

        _refresh_paused_row_stamp = _bind_deps(
            _core_saver_state._refresh_paused_row_stamp,
            _paused_row_last_hold_mono=_paused_row_last_hold_mono,
            _paused_row_since_mono=_paused_row_since_mono,
            _pausesaver_is_holding=_pausesaver_is_holding,
        )

        _clock_saver_content_is_idle = _bind_deps(
            _core_saver_state._clock_saver_content_is_idle,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _clock_startup_intro_opacity = _bind_deps(
            _core_startup._clock_startup_intro_opacity,
            CLOCK_STARTUP_FADE_S=CLOCK_STARTUP_FADE_S,
            clock_saver_composite_bgra=clock_saver_composite_bgra,
            post_splash_mono=post_splash_mono,
            startup_ph=startup_ph,
        )

        _stage_grid_overlay_mode = _bind_deps(
            _core_stage_render._stage_grid_overlay_mode,
            DisplayView=DisplayView,
            display_view_holder=display_view_holder,
            view_five_mode_holder=view_five_mode_holder,
        )

        def _clock_saver_for_compose(now: float) -> bool:
            """True when the large saver time/date patches should be drawn (idle path)."""
            if clock_saver_composite_bgra is None:
                return False
            if _clock_startup_intro_opacity(now) is not None:
                if _tmdb_info_current_and_available():
                    return False
                return True
            if dev_phase[0] != DevPhase.OFF:
                return False
            # Splash overlay: keep underlay black until reveal frame, then paint clock under PNG alpha.
            if startup_ph[0] is not None:
                return bool(_splash_reveal_clock[0])
            ev = _effective_display_view()
            if ev == DisplayView.FOUR:
                return False
            # View ONE now-playing may run with scene off; still allow the idle saver.
            if (not scene_enabled[0]) and ev != DisplayView.ONE:
                return False
            try:
                plan = _apply_auto_widget_policy()
                from pigeon.auto_widgets import (
                    LAYOUT_SETTINGS,
                    LAYOUT_ZONE6_CLOCKSAVER,
                    LAYOUT_ZONE6_PAUSESAVER,
                    LAYOUT_ZONE8_CLOCKSAVER,
                    LAYOUT_ZONE10_PAUSESAVER,
                )

                if plan.force_settings or plan.layout == LAYOUT_SETTINGS:
                    return False
                if plan.layout == LAYOUT_ZONE8_CLOCKSAVER:
                    return True
                if plan.layout in (
                    LAYOUT_ZONE6_PAUSESAVER,
                    LAYOUT_ZONE10_PAUSESAVER,
                ):
                    from pigeon.clock_saver_policy import pausesaver_due_for_clocksaver

                    if pausesaver_due_for_clocksaver(_refresh_paused_row_stamp(now)):
                        return True
                    return False
                if plan.layout == LAYOUT_ZONE6_CLOCKSAVER:
                    return False
            except Exception:
                pass
            if clock_saver_force_on[0]:
                return True
            return _clock_saver_active(now)

        _idle_saver_face_toggle_ok = _bind_deps(
            _core_saver_state._idle_saver_face_toggle_ok,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _clock_startup_intro_opacity=_clock_startup_intro_opacity,
            startup_ph=startup_ph,
        )

        _idle_audio_meter_active = _bind_deps(
            _core_saver_state._idle_audio_meter_active,
            _idle_saver_face_toggle_ok=_idle_saver_face_toggle_ok,
            audio_meter_face_enabled=audio_meter_face_enabled,
        )

        _program_audio_present = _bind_deps(
            _core_now_playing._program_audio_present,
            program_audio_present=program_audio_present,
        )

        _program_audio_session = _bind_deps(
            _core_now_playing._program_audio_session,
            _program_audio_present=_program_audio_present,
            program_audio_session_present=program_audio_session_present,
        )

        _idle_audio_listen = _bind_deps(
            _core_saver_state._idle_audio_listen,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _view_one_uses_now_playing_screen=_late(lambda: _view_one_uses_now_playing_screen, "_view_one_uses_now_playing_screen"),
        )

        def _settings_audio_led_listen() -> bool:
            """Keep ALSA open on settings_pigeon so the audio LED can follow signal."""
            if main_settings_widget is None:
                return False
            try:
                st = main_settings_widget.state
            except Exception:
                return False
            if not bool(getattr(st, "show_pigeon_settings", False)):
                return False
            if bool(getattr(st, "show_widgets", False)):
                return False
            if bool(getattr(st, "show_options", False)):
                return False
            if bool(getattr(st, "show_ui_color", False)):
                return False
            if bool(getattr(st, "show_preferences", False)):
                return False
            if bool(getattr(st, "show_metadata_debug", False)):
                return False
            return True

        def _np_wants_live_audio() -> bool:
            if view_circles_widget is None:
                return False
            if not _view_one_uses_now_playing_screen():
                return False
            try:
                if _clock_saver_for_compose(time.monotonic()):
                    return False
            except Exception:
                pass
            try:
                return bool(view_circles_widget.wants_live_audio())
            except Exception:
                return False

        _audio_capture_wanted = _bind_deps(
            _core_saver_state._audio_capture_wanted,
            _idle_audio_listen=_idle_audio_listen,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _np_wants_live_audio=_np_wants_live_audio,
            _settings_audio_led_listen=_settings_audio_led_listen,
        )

        _toggle_clock_saver_force = _bind_deps(
            _core_saver_state._toggle_clock_saver_force,
            _PIGEON_EXT=_PIGEON_EXT,
            _boot_clock_saver_until_playback=_boot_clock_saver_until_playback,
            _bump_clock_saver_significant_device=_bump_clock_saver_significant_device,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _note_metadata_activity=_note_metadata_activity,
            _widget_accepts_typing=_widget_accepts_typing,
            clock_saver_composite_bgra=clock_saver_composite_bgra,
            clock_saver_force_on=clock_saver_force_on,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
        )

        _clock_saver_dim_pre_digit_canvas = _core_saver_state._clock_saver_dim_pre_digit_canvas

        _clock_saver_dim_overlay_bgra = _core_saver_state._clock_saver_dim_overlay_bgra

        idle_dim_anim_strength = [0.0]
        _idle_dim_anim_goal = [0.0]
        _idle_dim_anim_from = [0.0]
        _idle_dim_anim_t0 = [time.monotonic()]

        _update_idle_dim_strength = _bind_deps(
            _core_saver_state._update_idle_dim_strength,
            ATV_IDLE_MONO_ANIM_S=ATV_IDLE_MONO_ANIM_S,
            THEATER_IDLE_DIM_ENABLED=THEATER_IDLE_DIM_ENABLED,
            _atv_idle_monochrome_active=_atv_idle_monochrome_active,
            _idle_dim_anim_from=_idle_dim_anim_from,
            _idle_dim_anim_goal=_idle_dim_anim_goal,
            _idle_dim_anim_t0=_idle_dim_anim_t0,
            idle_dim_anim_strength=idle_dim_anim_strength,
        )

        _compose_idle_strength_holder: list[float] = [0.0]
        _live_perf: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0]
        _hitch_parts: list[float] = [0.0, 0.0]
        _np_state_sync_mono: list[float] = [0.0]
        _np_dump_mono: list[float] = [0.0]

        _record_live_audio_timing = _bind_deps(
            _core_stage_render._record_live_audio_timing,
            _hitch_parts=_hitch_parts,
            _live_perf=_live_perf,
        )

        scaled_display: list[np.ndarray | None] = [None]
        scaled_version = [0]
        if last_frame[0] is not None:
            scaled_display[0] = _disp_fit().scale_and_crop(last_frame[0])
            scaled_version[0] = 1

        _nav_coalescer_holder: list[object] = [None]
        _nav_request: list[object] = [None]
        scene_enabled = [_load_persisted_scene_enabled(True)]

        _effective_display_view = _bind_deps(
            _core_stage_render._effective_display_view,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
        )

        _design_grid_overlay_active = _bind_deps(
            _core_stage_render._design_grid_overlay_active,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
        )

        # Advanced matrix: temporarily show GRID behind the dialog when opened from Settings; restore on close.
        advanced_matrix_restore_phase: list[object] = [None]
        advanced_matrix_close_skip: list[bool] = [False]
        location_toast_state: dict[str, object] = {
            "active": False,
            "text": "",
            "t0": 0.0,
            "startup_top_left": False,
        }
        prev_dev_phase_for_location_toast: list[DevPhase] = [DevPhase.OFF]
        clock_saver_peek_until_mono: list[float] = [0.0]

        _clock_saver_layer_opacity = _bind_deps(
            _core_saver_state._clock_saver_layer_opacity,
            CLOCK_SAVER_DIM_OPACITY=CLOCK_SAVER_DIM_OPACITY,
            _boot_clock_saver_until_playback=_boot_clock_saver_until_playback,
            _clock_startup_intro_opacity=_clock_startup_intro_opacity,
            _splash_reveal_clock=_splash_reveal_clock,
            clock_saver_peek_until_mono=clock_saver_peek_until_mono,
        )

        black_photo: list[ImageTk.PhotoImage | None] = [None]
        label_live_photo: list[ImageTk.PhotoImage | None] = [None]
        _render_after_id: list[str | None] = [None]
        use_backdrop_scene = [False]

        _backdrop_active_for_view = _bind_deps(
            _core_stage_render._backdrop_active_for_view,
            DisplayView=DisplayView,
            _effective_display_view=_effective_display_view,
            use_backdrop_scene=use_backdrop_scene,
        )

        backdrop_master_bgr: list[np.ndarray | None] = [None]

        _clock_saver_backdrop_brightness = _bind_deps(
            _core_saver_state._clock_saver_backdrop_brightness,
            CLOCK_SAVER_BACKDROP_DIM=CLOCK_SAVER_BACKDROP_DIM,
            _backdrop_active_for_view=_backdrop_active_for_view,
            _clock_saver_for_compose=_clock_saver_for_compose,
            backdrop_master_bgr=backdrop_master_bgr,
        )

        # Last TMDb backdrop (copy); survives display off so developer-grid F10 can return to backdrop.
        saved_backdrop_master_bgr: list[np.ndarray | None] = [None]
        # True when the saved/current master came from the streaming app logo (not TMDb stills).
        saved_backdrop_app_logo_letterbox_fit: list[bool] = [False]
        backdrop_app_logo_letterbox_fit: list[bool] = [False]

        _app_logo_clock_saver_style_now = _bind_deps(
            _core_saver_state._app_logo_clock_saver_style_now,
            _clock_saver_for_compose=_clock_saver_for_compose,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
        )

        if _PIGEON_EXT and prepare_default_poster_at_startup is not None:
            try:
                ok_sp, msg_sp, _gc_sp = prepare_default_poster_at_startup()
                sys.stderr.write(f"pigeon: startup poster: {msg_sp}\n")
                sys.stderr.flush()
            except Exception as e:
                sys.stderr.write(f"pigeon: startup poster error: {e}\n")
                sys.stderr.flush()

        clock_widget = (
            ClockCalendarWidget(
                anchor_row=CLOCK_WIDGET_ROW,
                anchor_col_right=float(VIEW_ONE_CLOCK_COL_RIGHT),
            )
            if _PIGEON_EXT and ClockCalendarWidget is not None
            else None
        )
        info_cluster_clock_widget = (
            ClockCalendarWidget(
                anchor_row=float(INFO_CLUSTER_CLOCK_ROW_1BASED),
                anchor_col_right=float(INFO_CLUSTER_COL_RIGHT),
                placement="overlay",
            )
            if _PIGEON_EXT and ClockCalendarWidget is not None
            else None
        )
        tmdb_logo_widget = (
            TmdbLogoWidget(
                anchor_row=TMDB_LOGO_ANCHOR_ROW,
                anchor_col=0,
                span_wide=TMDB_LOGO_SPAN_W,
                span_tall=TMDB_LOGO_SPAN_H,
                fit_scale=TMDB_LOGO_FIT_SCALE,
                vertical_align="top",
                top_right_col_1based=float(TMDB_LOGO_TOP_RIGHT_COL),
            )
            if _PIGEON_EXT and TmdbLogoWidget is not None
            else None
        )
        tmdb_logo_widget_view_six = (
            TmdbLogoWidget(
                anchor_row=TMDB_LOGO_VIEW6_ANCHOR_ROW,
                anchor_col=TMDB_LOGO_VIEW6_ANCHOR_COL,
                span_wide=TMDB_LOGO_VIEW6_SPAN_W,
                span_tall=TMDB_LOGO_VIEW6_SPAN_H,
                fit_scale=TMDB_LOGO_VIEW6_FIT_SCALE,
                vertical_align="center",
            )
            if _PIGEON_EXT and TmdbLogoWidget is not None
            else None
        )
        # When TMDb returns no match for the current playback title, surface the streaming
        # app's own logo in the content-logo slot instead of popping an error dialog.
        # ``_warm_tmdb_logo_patch`` consults this flag whenever no TMDb logo is active.
        tmdb_logo_app_fallback_active: list[bool] = [False]
        # Contrast-aware bottom gradient tint: ``_warm_tmdb_logo_patch`` evaluates the active
        # TT logo via ``pigeon.tmdb_tt_contrast.pick_gradient_bgr`` and stores the winning
        # ``(B, G, R)`` here. Default black preserves legacy behaviour when no TT is loaded.
        tmdb_tt_gradient_bgr_holder: list[tuple[int, int, int]] = [GRADIENT_BGR_DARK]
        status_bar_widget = (
            StatusBarWidget(
                assets_dir=Path(_PROJECT_DIR) / "pigeonAssets",
                trt_row=TRT_DISPLAY_ROW,
                trt_played_col=TRT_PLAYED_COL,
                trt_remaining_col=TRT_REMAINING_COL,
                trt_played_text=TRT_PLAYED_TEXT,
                trt_remaining_text=TRT_REMAINING_TEXT,
                trt_label_span_wide=TRT_LABEL_SPAN_W,
                trt_label_span_tall=TRT_LABEL_SPAN_H,
            )
            if _PIGEON_EXT and StatusBarWidget is not None
            else None
        )

        receiver_overlay_state: dict[str, str] = {
            "incoming": "",
            "config": "",
            "volume": "",
            "input": "",
        }
        receiver_telnet_debug_holder: list[dict[str, str]] = [{}]

        _denon_telnet_audio_fallback = _bind_deps(
            _core_device_control._denon_telnet_audio_fallback,
            receiver_telnet_debug_holder=receiver_telnet_debug_holder,
        )

        # Track the last usable Denon volume reading so the Apple TV metadata poll (which
        # reports ``volume_percent=0`` when an AV receiver owns the volume line) does not
        # briefly overwrite the authoritative dB value on its own cadence. The receiver
        # poll keeps running on its own schedule; this cache only controls *display*.
        denon_vol_cache: dict[str, object] = {
            "effective": "",
            "mono_usable": 0.0,
            # Last volume string shown on View 1 (survives brief empty polls).
            "np_hold": "",
            "heal_quick_mono": 0.0,
            "heal_sweep_mono": 0.0,
            "bound_host": "",
        }
        # True when the last Denon poll answered but reported OFF/STANDBY — hide all
        # receiver metadata and treat the receiver indicator as inactive.
        receiver_standby_holder: list[bool] = [False]

        _resolve_receiver_lines_for_now_playing = _bind_deps(
            _core_device_control._resolve_receiver_lines_for_now_playing,
            _clock_saver_volume_raw=_clock_saver_volume_raw,
            _denon_telnet_audio_fallback=_denon_telnet_audio_fallback,
            apple_tv_auto_state=apple_tv_auto_state,
            compose_playback_volume_widget_line=compose_playback_volume_widget_line,
            denon_vol_cache=denon_vol_cache,
            receiver_overlay_state=receiver_overlay_state,
            receiver_standby_holder=receiver_standby_holder,
            streaming_slot_holder=streaming_slot_holder,
        )

        _clock_saver_active = _bind_deps(
            _core_saver_state._clock_saver_active,
            CLOCK_SAVER_METADATA_IDLE_AFTER_S=CLOCK_SAVER_METADATA_IDLE_AFTER_S,
            DevPhase=DevPhase,
            _apple_tv_is_off=_apple_tv_is_off,
            _apply_position_stall_grace_to_clock_saver=_apply_position_stall_grace_to_clock_saver,
            _boot_clock_saver_until_playback=_boot_clock_saver_until_playback,
            _clock_saver_content_is_idle=_clock_saver_content_is_idle,
            _clock_saver_idle_need=_clock_saver_idle_need,
            _clock_saver_user_enabled=_clock_saver_user_enabled,
            _cs_meta_idle_log_mono=_cs_meta_idle_log_mono,
            _cs_meta_idle_was_active=_cs_meta_idle_was_active,
            _metadata_drives_clock_saver=_metadata_drives_clock_saver,
            _note_metadata_activity=_note_metadata_activity,
            _pausesaver_is_holding=_pausesaver_is_holding,
            _program_audio_session=_program_audio_session,
            _refresh_paused_row_stamp=_refresh_paused_row_stamp,
            _resolve_receiver_lines_for_now_playing=_resolve_receiver_lines_for_now_playing,
            _something_playing_now=_something_playing_now,
            _splash_reveal_clock=_splash_reveal_clock,
            _tmdb_info_current_and_available=_tmdb_info_current_and_available,
            apple_tv_playback_clock=apple_tv_playback_clock,
            clock_saver_composite_bgra=clock_saver_composite_bgra,
            dev_phase=dev_phase,
            last_clock_saver_significant_device_mono=last_clock_saver_significant_device_mono,
            last_metadata_activity_mono=last_metadata_activity_mono,
            last_pigeon_user_activity_mono=last_pigeon_user_activity_mono,
            receiver_standby_holder=receiver_standby_holder,
            scene_enabled=scene_enabled,
            startup_ph=startup_ph,
        )

        _resolve_receiver_input_label = _bind_deps(
            _core_device_control._resolve_receiver_input_label,
            receiver_overlay_state=receiver_overlay_state,
            receiver_standby_holder=receiver_standby_holder,
            receiver_telnet_debug_holder=receiver_telnet_debug_holder,
        )

        _np_widgets_content_active = _bind_deps(
            _core_now_playing._np_widgets_content_active,
            _apple_tv_is_off=_apple_tv_is_off,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _program_audio_session=_program_audio_session,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _something_playing_now=_something_playing_now,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            receiver_standby_holder=receiver_standby_holder,
        )

        # Volume knob requested PWON; keep sending power-on even if a poll
        # briefly reports STANDBY again before the AVR finishes waking.
        receiver_power_on_pending: list[bool] = [False]
        receiver_power_on_until: list[float] = [0.0]
        receiver_volume_cmd_busy: list[bool] = [False]
        playback_overlay_flags: dict[str, bool] = {
            "show_paused_row": False,
            "clock_saver_volume_only": False,
            "clock_saver_netflix_full_overlay": False,
            "badge_live_instead_of_logo": False,
        }
        playback_overlay_widget = (
            PlaybackOverlayWidget(
                assets_dir=Path(_PROJECT_DIR) / "pigeonAssets",
                receiver_state=receiver_overlay_state,
                service_badge=streaming_badge_state,
                overlay_flags=playback_overlay_flags,
                badge_top_right_col_1based=VIEW_ONE_BADGE_COL_RIGHT,
                volume_top_right_col_1based=float(VIEW_ONE_CLOCK_COL_RIGHT),
            )
            if _PIGEON_EXT and PlaybackOverlayWidget is not None
            else None
        )

        # Now-playing: five-zone circles skin only.
        view_circles_widget = (
            ViewCirclesWidget(assets_dir=Path(_PROJECT_DIR) / "pigeonAssets")
            if _PIGEON_EXT and ViewCirclesWidget is not None
            else None
        )
        if view_circles_widget is not None:
            try:
                from pigeon.widgets.preferences_settings import (
                    ensure_now_playing_layout_defaults,
                )

                ensure_now_playing_layout_defaults()
            except Exception:
                pass
        main_settings_widget = (
            MainSettingsWidget(assets_dir=Path(_PROJECT_DIR) / "pigeonAssets")
            if _PIGEON_EXT and MainSettingsWidget is not None
            else None
        )
        if main_settings_widget is not None:
            try:
                main_settings_widget.state.version_string = version_string()
                main_settings_widget.state.update_local_version = version_string()
            except Exception:
                pass

        _view_one_uses_now_playing_screen = _bind_deps(
            _core_view_one._view_one_uses_now_playing_screen,
            DisplayView=DisplayView,
            _effective_display_view=_effective_display_view,
            view_circles_widget=view_circles_widget,
        )

        _np_drawing_live_audio = _bind_deps(
            _core_now_playing._np_drawing_live_audio,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            view_circles_widget=view_circles_widget,
        )

        _settings_is_native_1280 = _bind_deps(
            _core_settings_ui._settings_is_native_1280,
            DevPhase=DevPhase,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
        )

        _settings_menu_is_static = _bind_deps(
            _core_settings_ui._settings_menu_is_static,
            DevPhase=DevPhase,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
        )

        _composite_settings_on_canvas = _bind_deps(
            _core_settings_ui._composite_settings_on_canvas,
            _nav_coalescer_holder=_nav_coalescer_holder,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _something_playing_now=_something_playing_now,
            _sync_now_playing_screen_state=_late(lambda: _sync_now_playing_screen_state, "_sync_now_playing_screen_state"),
            _sync_preferences_now_playing_progress=_sync_preferences_now_playing_progress,
            _sync_settings_zone2_tt=_late(lambda: _sync_settings_zone2_tt, "_sync_settings_zone2_tt"),
            main_settings_widget=main_settings_widget,
            view_circles_widget=view_circles_widget,
        )

        _sync_now_playing_screen_state = _bind_deps(
            _core_now_playing._sync_now_playing_screen_state,
            DisplayView=DisplayView,
            _active_tmdb_tt_src_bgra=_late(lambda: _active_tmdb_tt_src_bgra, "_active_tmdb_tt_src_bgra"),
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _circles_poster_bgra=_late(lambda: _circles_poster_bgra, "_circles_poster_bgra"),
            _effective_display_view=_effective_display_view,
            _format_hmmss=_format_hmmss,
            _has_playback_position=_has_playback_position,
            _np_dump_mono=_np_dump_mono,
            _np_widgets_content_active=_np_widgets_content_active,
            _paused_screen_backdrop_bgr=_late(lambda: _paused_screen_backdrop_bgr, "_paused_screen_backdrop_bgr"),
            _pausesaver_is_holding=_pausesaver_is_holding,
            _playback_extrapolated_pair=_late(lambda: _playback_extrapolated_pair, "_playback_extrapolated_pair"),
            _playback_progress_fraction_for_bar=_late(lambda: _playback_progress_fraction_for_bar, "_playback_progress_fraction_for_bar"),
            _refresh_paused_row_stamp=_refresh_paused_row_stamp,
            _resolve_receiver_input_label=_resolve_receiver_input_label,
            _resolve_receiver_lines_for_now_playing=_resolve_receiver_lines_for_now_playing,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _spawn_youtube_thumb_fetch=_late(lambda: _spawn_youtube_thumb_fetch, "_spawn_youtube_thumb_fetch"),
            _vv_is_music=_late(lambda: _vv_is_music, "_vv_is_music"),
            _vv_is_youtube=_late(lambda: _vv_is_youtube, "_vv_is_youtube"),
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            receiver_standby_holder=receiver_standby_holder,
            skip_cache=skip_cache,
            streaming_badge_state=streaming_badge_state,
            view_circles_widget=view_circles_widget,
        )

        _sync_now_playing_screen_state_for_frame = _bind_deps(
            _core_now_playing._sync_now_playing_screen_state_for_frame,
            _np_state_sync_mono=_np_state_sync_mono,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            view_circles_widget=view_circles_widget,
        )

        _clear_now_playing_view_caches = _bind_deps(
            _core_now_playing._clear_now_playing_view_caches,
            view_circles_widget=view_circles_widget,
        )

        _enable_now_playing_screen = _bind_deps(
            _core_now_playing._enable_now_playing_screen,
            DisplayView=DisplayView,
            LANDING_DISPLAY_BRIGHTNESS=LANDING_DISPLAY_BRIGHTNESS,
            _PIGEON_EXT=_PIGEON_EXT,
            _sync_status_bar_visibility_for_playback=_late(lambda: _sync_status_bar_visibility_for_playback, "_sync_status_bar_visibility_for_playback"),
            apple_tv_auto_state=apple_tv_auto_state,
            brightness_current=brightness_current,
            brightness_from=brightness_from,
            brightness_target=brightness_target,
            display_view_holder=display_view_holder,
            last_frame=last_frame,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            view_circles_widget=view_circles_widget,
        )

        _activate_now_playing_after_splash = _bind_deps(
            _core_startup._activate_now_playing_after_splash,
            _enable_now_playing_screen=_enable_now_playing_screen,
            _startup_splash_complete=_startup_splash_complete,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
        )

        _post_splash_startup_hook[0] = _activate_now_playing_after_splash

        _splash_view_one_warm_done: list[bool] = [False]

        _log_view_one_startup_phase = _bind_deps(
            _core_view_one._log_view_one_startup_phase,
            _app_startup_mono=_app_startup_mono,
        )

        _warm_view_one_splash_chrome_only = _bind_deps(
            _core_startup._warm_view_one_splash_chrome_only,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            _log_view_one_startup_phase=_log_view_one_startup_phase,
            display_view_holder=display_view_holder,
            root=root,
            view_circles_widget=view_circles_widget,
        )

        root.after(150, lambda: _warm_view_one_splash_chrome_only(phase="chrome-early"))

        _playback_is_netflix_stream = _bind_deps(
            _core_now_playing._playback_is_netflix_stream,
            _metadata_is_netflix_app=_metadata_is_netflix_app,
            apple_tv_auto_state=apple_tv_auto_state,
            streaming_badge_state=streaming_badge_state,
        )

        _backdrop_master_from_streaming_app_logo = _bind_deps(
            _core_view_one._backdrop_master_from_streaming_app_logo,
            APP_LOGO_FALLBACK_MAX_RESOLUTION_FRACTION=APP_LOGO_FALLBACK_MAX_RESOLUTION_FRACTION,
            _PROJECT_DIR=_PROJECT_DIR,
            _metadata_is_netflix_app=_metadata_is_netflix_app,
            apple_tv_auto_state=apple_tv_auto_state,
            display_dims=display_dims,
            streaming_badge_state=streaming_badge_state,
        )

        _apply_netflix_backdrop_when_running = _bind_deps(
            _core_stage_render._apply_netflix_backdrop_when_running,
            BACKDROP_BRIGHTNESS=BACKDROP_BRIGHTNESS,
            _backdrop_master_from_streaming_app_logo=_backdrop_master_from_streaming_app_logo,
            _playback_is_netflix_stream=_playback_is_netflix_stream,
            _save_persisted_scene_enabled=_save_persisted_scene_enabled,
            _warm_status_bar_blits=_late(lambda: _warm_status_bar_blits, "_warm_status_bar_blits"),
            _warm_tmdb_logo_patch=_late(lambda: _warm_tmdb_logo_patch, "_warm_tmdb_logo_patch"),
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            brightness_current=brightness_current,
            brightness_from=brightness_from,
            brightness_t0=brightness_t0,
            brightness_target=brightness_target,
            cap=cap,
            current_apple_tv=current_apple_tv,
            last_frame=last_frame,
            playing=playing,
            saved_backdrop_app_logo_letterbox_fit=saved_backdrop_app_logo_letterbox_fit,
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
            streaming_badge_state=streaming_badge_state,
            tmdb_logo_app_fallback_active=tmdb_logo_app_fallback_active,
            tmdb_logo_patch_bgra=tmdb_logo_patch_bgra,
            tmdb_logo_widget=tmdb_logo_widget,
            tmdb_logo_widget_view_six=tmdb_logo_widget_view_six,
            use_backdrop_scene=use_backdrop_scene,
        )

        _pigeon_ui_started_mono = time.monotonic()

        clock_patch_bgra: list[np.ndarray | None] = [None]
        status_bar_blits: list[list] = [[]]
        playback_overlay_blits: list[list] = [[]]
        info_cluster_blits: list[list] = [[]]
        _info_cluster_blits_sig: list[object | None] = [None]
        # [unix_sec, status_bar accent BGR or None] — clock patch invalidation.
        _clock_patch_sig: list = [-1, None]
        _blend_top_gradient_design = _core_stage_render._blend_top_gradient_design

        _blend_top_gradient_fast = _core_stage_render._blend_top_gradient_fast

        _apply_stage_chrome_colors = _bind_deps(
            _core_stage_render._apply_stage_chrome_colors,
            label=label,
            video_area=video_area,
        )

        _refresh_stage_from_poster = _bind_deps(
            _core_stage_render._refresh_stage_from_poster,
            _PIGEON_EXT=_PIGEON_EXT,
            _apply_stage_chrome_colors=_apply_stage_chrome_colors,
            black_photo=black_photo,
            skip_cache=skip_cache,
        )

        _refresh_stage_from_poster()

        _design_rect_to_target = _bind_deps(
            _core_stage_render._design_rect_to_target,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
        )

        _design_rect_to_window = _bind_deps(
            _core_stage_render._design_rect_to_window,
            _design_rect_to_target=_design_rect_to_target,
            display_dims=display_dims,
        )

        _saver_layer_is_full_frame = _core_saver_state._saver_layer_is_full_frame

        _blit_saver_layers_design = _bind_deps(
            _core_saver_state._blit_saver_layers_design,
            _saver_layer_is_full_frame=_saver_layer_is_full_frame,
            alpha_blend_bgra_over_bgr=alpha_blend_bgra_over_bgr,
        )

        _blit_saver_layers_target = _bind_deps(
            _core_saver_state._blit_saver_layers_target,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            _design_rect_to_target=_design_rect_to_target,
            alpha_blend_bgra_over_bgr=alpha_blend_bgra_over_bgr,
        )

        _warm_status_bar_blits = _bind_deps(
            _core_now_playing._warm_status_bar_blits,
            status_bar_blits=status_bar_blits,
            status_bar_widget=status_bar_widget,
        )

        _warm_view_one_under_splash = _bind_deps(
            _core_startup._warm_view_one_under_splash,
            _PIGEON_EXT=_PIGEON_EXT,
            _enable_now_playing_screen=_enable_now_playing_screen,
            _log_view_one_startup_phase=_log_view_one_startup_phase,
            _splash_view_one_warm_done=_splash_view_one_warm_done,
            _warm_status_bar_blits=_warm_status_bar_blits,
            root=root,
            view_circles_widget=view_circles_widget,
        )

        _set_playback_overlay_clock_saver_volume_flag = _bind_deps(
            _core_saver_state._set_playback_overlay_clock_saver_volume_flag,
            _backdrop_active_for_view=_backdrop_active_for_view,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _playback_is_netflix_stream=_playback_is_netflix_stream,
            playback_overlay_flags=playback_overlay_flags,
            receiver_overlay_state=receiver_overlay_state,
        )

        _warm_playback_overlay_blits = _bind_deps(
            _core_stage_render._warm_playback_overlay_blits,
            _set_playback_overlay_clock_saver_volume_flag=_set_playback_overlay_clock_saver_volume_flag,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _view_one_streaming_logo_duplicate_fallback=_late(lambda: _view_one_streaming_logo_duplicate_fallback, "_view_one_streaming_logo_duplicate_fallback"),
            _warm_info_cluster_blits=_late(lambda: _warm_info_cluster_blits, "_warm_info_cluster_blits"),
            playback_overlay_blits=playback_overlay_blits,
            playback_overlay_flags=playback_overlay_flags,
            playback_overlay_widget=playback_overlay_widget,
        )

        _info_cluster_compose_active = _bind_deps(
            _core_stage_render._info_cluster_compose_active,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _effective_display_view=_effective_display_view,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            dev_phase=dev_phase,
        )

        _warm_info_cluster_blits = _bind_deps(
            _core_stage_render._warm_info_cluster_blits,
            _PIGEON_EXT=_PIGEON_EXT,
            _info_cluster_blits_sig=_info_cluster_blits_sig,
            _info_cluster_compose_active=_info_cluster_compose_active,
            _location_toast_alpha=_late(lambda: _location_toast_alpha, "_location_toast_alpha"),
            build_info_cluster_design_patches=build_info_cluster_design_patches,
            info_cluster_blits=info_cluster_blits,
            info_cluster_clock_widget=info_cluster_clock_widget,
            location_toast_state=location_toast_state,
            receiver_overlay_state=receiver_overlay_state,
            status_bar_widget=status_bar_widget,
        )

        _blend_info_cluster_into_target = _bind_deps(
            _core_stage_render._blend_info_cluster_into_target,
            _design_rect_to_target=_design_rect_to_target,
            _info_cluster_compose_active=_info_cluster_compose_active,
            _warm_info_cluster_blits=_warm_info_cluster_blits,
            alpha_blend_bgra_over_bgr=alpha_blend_bgra_over_bgr,
            info_cluster_blits=info_cluster_blits,
        )

        _active_tmdb_logo_widget = _bind_deps(
            _core_view_one._active_tmdb_logo_widget,
            DisplayView=DisplayView,
            _effective_display_view=_effective_display_view,
            tmdb_logo_widget=tmdb_logo_widget,
            tmdb_logo_widget_view_six=tmdb_logo_widget_view_six,
        )

        _tmdb_poster_cache: dict[str, object] = {"key": None, "bgra": None}
        _tmdb_tt_src_cache: dict[str, object] = {"key": None, "bgra": None}

        _styled_video_content_c_poster = _core_stage_render._styled_video_content_c_poster

        _active_tmdb_poster_bgra = _bind_deps(
            _core_tmdb_flow._active_tmdb_poster_bgra,
            _tmdb_poster_cache=_tmdb_poster_cache,
            active_tmdb_title_key=active_tmdb_title_key,
        )

        _active_tmdb_tt_src_bgra = _bind_deps(
            _core_tmdb_flow._active_tmdb_tt_src_bgra,
            _tmdb_tt_src_cache=_tmdb_tt_src_cache,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
        )

        _sync_settings_zone2_tt = _bind_deps(
            _core_settings_ui._sync_settings_zone2_tt,
            main_settings_widget=main_settings_widget,
        )

        _clear_music_artwork_cache = _bind_deps(
            _core_now_playing._clear_music_artwork_cache,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _clear_video_artwork_cache = _bind_deps(
            _core_now_playing._clear_video_artwork_cache,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _clear_playback_artwork_caches = _bind_deps(
            _core_now_playing._clear_playback_artwork_caches,
            _clear_music_artwork_cache=_clear_music_artwork_cache,
            _clear_video_artwork_cache=_clear_video_artwork_cache,
        )

        _music_artwork_track_key = _core_now_playing._music_artwork_track_key

        _decode_artwork_bytes_bgra = _core_now_playing._decode_artwork_bytes_bgra

        _circles_poster_bgra = _bind_deps(
            _core_now_playing._circles_poster_bgra,
            _active_tmdb_poster_bgra=_active_tmdb_poster_bgra,
            _vv_is_music=_late(lambda: _vv_is_music, "_vv_is_music"),
            _vv_is_youtube=_late(lambda: _vv_is_youtube, "_vv_is_youtube"),
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _spawn_youtube_thumb_fetch = _bind_deps(
            _core_now_playing._spawn_youtube_thumb_fetch,
            _store_music_artwork_from_metadata=_late(lambda: _store_music_artwork_from_metadata, "_store_music_artwork_from_metadata"),
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            apple_tv_auto_state=apple_tv_auto_state,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            skip_cache=skip_cache,
        )

        _resolve_streaming_app_logo_bgra = _bind_deps(
            _core_view_one._resolve_streaming_app_logo_bgra,
            _PROJECT_DIR=_PROJECT_DIR,
            apple_tv_auto_state=apple_tv_auto_state,
            streaming_badge_state=streaming_badge_state,
        )

        _refresh_tmdb_tt_gradient_tint = _bind_deps(
            _core_tmdb_flow._refresh_tmdb_tt_gradient_tint,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            tmdb_logo_patch_bgra=tmdb_logo_patch_bgra,
            tmdb_tt_gradient_bgr_holder=tmdb_tt_gradient_bgr_holder,
        )

        _warm_tmdb_logo_patch = _bind_deps(
            _core_tmdb_flow._warm_tmdb_logo_patch,
            _active_tmdb_logo_widget=_active_tmdb_logo_widget,
            _refresh_tmdb_tt_gradient_tint=_refresh_tmdb_tt_gradient_tint,
            _resolve_streaming_app_logo_bgra=_resolve_streaming_app_logo_bgra,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            tmdb_logo_app_fallback_active=tmdb_logo_app_fallback_active,
            tmdb_logo_patch_bgra=tmdb_logo_patch_bgra,
        )

        # ---- View 1 fallback-variant detection (viewOne.01 .. .09) --------
        # These probe live state so ``_current_view_one_variant`` can route the
        # View-1 composition path through the correct fallback. See
        # ``pigeon/view_one_variants.py`` for the decision table.
        _playback_display_title = _bind_deps(
            _core_now_playing._playback_display_title,
            active_tmdb_display_title=active_tmdb_display_title,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _vv_has_content_title = _bind_deps(
            _core_now_playing._vv_has_content_title,
            _playback_display_title=_playback_display_title,
        )

        _vv_has_current_app = _bind_deps(
            _core_now_playing._vv_has_current_app,
            apple_tv_auto_state=apple_tv_auto_state,
            streaming_badge_state=streaming_badge_state,
        )

        _vv_has_tmdb_bd = _bind_deps(
            _core_tmdb_flow._vv_has_tmdb_bd,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            saved_backdrop_app_logo_letterbox_fit=saved_backdrop_app_logo_letterbox_fit,
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
        )

        _vv_has_tmdb_tt = _bind_deps(
            _core_tmdb_flow._vv_has_tmdb_tt,
            active_tmdb_title_key=active_tmdb_title_key,
        )

        _vv_has_app_logo = _bind_deps(
            _core_now_playing._vv_has_app_logo,
            _resolve_streaming_app_logo_bgra=_resolve_streaming_app_logo_bgra,
        )

        _vv_is_music = _bind_deps(
            _core_now_playing._vv_is_music,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _vv_is_youtube = _bind_deps(
            _core_now_playing._vv_is_youtube,
            apple_tv_auto_state=apple_tv_auto_state,
            streaming_badge_state=streaming_badge_state,
        )

        _store_music_artwork_from_metadata = _bind_deps(
            _core_now_playing._store_music_artwork_from_metadata,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _clear_music_artwork_cache=_clear_music_artwork_cache,
            _clear_playback_artwork_caches=_clear_playback_artwork_caches,
            _clear_video_artwork_cache=_clear_video_artwork_cache,
            _decode_artwork_bytes_bgra=_decode_artwork_bytes_bgra,
            _music_artwork_track_key=_music_artwork_track_key,
            _vv_is_youtube=_vv_is_youtube,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _vv_music_track_title = _bind_deps(
            _core_now_playing._vv_music_track_title,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _vv_music_text_lines = _bind_deps(
            _core_now_playing._vv_music_text_lines,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _current_view_one_variant = _bind_deps(
            _core_view_one._current_view_one_variant,
            ViewOneLayout=ViewOneLayout,
            _view_one_layout_effective=_view_one_layout_effective,
            _vv_has_app_logo=_vv_has_app_logo,
            _vv_has_content_title=_vv_has_content_title,
            _vv_has_current_app=_vv_has_current_app,
            _vv_has_tmdb_bd=_vv_has_tmdb_bd,
            _vv_has_tmdb_tt=_vv_has_tmdb_tt,
            resolve_view_one_variant=resolve_view_one_variant,
        )

        _view_one_streaming_logo_duplicate_fallback = _bind_deps(
            _core_view_one._view_one_streaming_logo_duplicate_fallback,
            ViewOneVariant=ViewOneVariant,
            _current_view_one_variant=_current_view_one_variant,
        )

        _view_one_variant_uses_full_path = _bind_deps(
            _core_view_one._view_one_variant_uses_full_path,
            _current_view_one_variant=_current_view_one_variant,
            _view_one_is_pigeon_full=_view_one_is_pigeon_full,
            _view_one_is_pigeon_poster=_view_one_is_pigeon_poster,
            variant_uses_full_path=variant_uses_full_path,
        )

        _view_one_variant_uses_simple_path = _bind_deps(
            _core_view_one._view_one_variant_uses_simple_path,
            _current_view_one_variant=_current_view_one_variant,
            _stage_is_view_one_video_layout=_stage_is_view_one_video_layout,
            _view_one_is_pigeon_poster=_view_one_is_pigeon_poster,
            _view_one_is_pigeon_simple=_view_one_is_pigeon_simple,
            variant_uses_full_path=variant_uses_full_path,
        )

        _view_one_video_content_a_tt_contain_rect_design = _bind_deps(
            _core_view_one._view_one_video_content_a_tt_contain_rect_design,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            PATCH_LAYER_RECEIVER_AUDIO=PATCH_LAYER_RECEIVER_AUDIO,
            VIEW_ONE_BADGE_COL_RIGHT=VIEW_ONE_BADGE_COL_RIGHT,
            get_grid_geometry=get_grid_geometry,
            playback_lower_gradient_bgra=playback_lower_gradient_bgra,
            playback_overlay_widget=playback_overlay_widget,
            rect_for_span_at_cell=rect_for_span_at_cell,
            rect_for_span_top_right_at_cell=rect_for_span_top_right_at_cell,
            tmdb_tt_gradient_bgr_holder=tmdb_tt_gradient_bgr_holder,
        )

        _paste_bgra_contain_on_design = _bind_deps(
            _core_stage_render._paste_bgra_contain_on_design,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            alpha_blend_bgra_over_bgr=alpha_blend_bgra_over_bgr,
        )

        _paused_screen_font_cache: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
        _bgr_from_bgra_cache: dict[str, object] = {"src_id": None, "bgr": None}

        _stable_bgr_from_bgra = _bind_deps(
            _core_now_playing._stable_bgr_from_bgra,
            _bgr_from_bgra_cache=_bgr_from_bgra_cache,
        )

        _paused_screen_artwork_bgr = _bind_deps(
            _core_now_playing._paused_screen_artwork_bgr,
            _stable_bgr_from_bgra=_stable_bgr_from_bgra,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _paused_screen_backdrop_bgr = _bind_deps(
            _core_saver_state._paused_screen_backdrop_bgr,
            _circles_poster_bgra=_circles_poster_bgra,
            _paused_screen_artwork_bgr=_paused_screen_artwork_bgr,
            _stable_bgr_from_bgra=_stable_bgr_from_bgra,
            _vv_is_music=_vv_is_music,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            saved_backdrop_app_logo_letterbox_fit=saved_backdrop_app_logo_letterbox_fit,
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
        )

        _auto_widget_signals = _bind_deps(
            _core_saver_state._auto_widget_signals,
            _apple_tv_is_off=_apple_tv_is_off,
            _clock_saver_receiver_off=_clock_saver_receiver_off,
            _paused_screen_backdrop_bgr=_paused_screen_backdrop_bgr,
            _player_metadata_class=_player_metadata_class,
            _program_audio_session=_program_audio_session,
            _refresh_paused_row_stamp=_refresh_paused_row_stamp,
            current_apple_tv=current_apple_tv,
            main_settings_widget=main_settings_widget,
            view_circles_widget=view_circles_widget,
        )

        _apply_auto_widget_policy = _bind_deps(
            _core_saver_state._apply_auto_widget_policy,
            DevPhase=DevPhase,
            _auto_widget_signals=_auto_widget_signals,
            _paused_screen_backdrop_bgr=_paused_screen_backdrop_bgr,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
            skip_cache=skip_cache,
        )

        _paused_screen_active = _bind_deps(
            _core_saver_state._paused_screen_active,
            DevPhase=DevPhase,
            _apply_auto_widget_policy=_apply_auto_widget_policy,
            _clock_saver_active=_clock_saver_active,
            _clock_saver_user_enabled=_clock_saver_user_enabled,
            _paused_screen_backdrop_bgr=_paused_screen_backdrop_bgr,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _vv_is_music=_vv_is_music,
            clock_saver_composite_bgra=clock_saver_composite_bgra,
            clock_saver_force_on=clock_saver_force_on,
            dev_phase=dev_phase,
        )

        _paused_screen_font = _bind_deps(
            _core_saver_state._paused_screen_font,
            _paused_screen_font_cache=_paused_screen_font_cache,
        )

        _compose_paused_screen = _bind_deps(
            _core_saver_state._compose_paused_screen,
            PAUSED_SCREEN_BACKDROP_DIM=PAUSED_SCREEN_BACKDROP_DIM,
            _paused_screen_backdrop_bgr=_paused_screen_backdrop_bgr,
            _paused_screen_font=_paused_screen_font,
        )

        _current_app_display_name = _bind_deps(
            _core_now_playing._current_app_display_name,
            apple_tv_auto_state=apple_tv_auto_state,
            streaming_badge_state=streaming_badge_state,
        )

        _refresh_clock_patch_bgra = _bind_deps(
            _core_stage_render._refresh_clock_patch_bgra,
            _clock_patch_sig=_clock_patch_sig,
            clock_patch_bgra=clock_patch_bgra,
            clock_widget=clock_widget,
            info_cluster_clock_widget=info_cluster_clock_widget,
            status_bar_widget=status_bar_widget,
        )

        _playback_overlay_fast_sig: list[tuple[bool, bool, bool, bool, bool] | None] = [None]
        _view1_canvas_bgr: list[np.ndarray | None] = [None]

        _acquire_view1_canvas = _bind_deps(
            _core_view_one._acquire_view1_canvas,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            _view1_canvas_bgr=_view1_canvas_bgr,
        )

        compose_display_fast_no_grid = _bind_deps(
            _core_stage_render.compose_display_fast_no_grid,
            CLOCK_ANCHOR_COL=CLOCK_ANCHOR_COL,
            CLOCK_ANCHOR_ROW=CLOCK_ANCHOR_ROW,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            PATCH_LAYER_RECEIVER_AUDIO=PATCH_LAYER_RECEIVER_AUDIO,
            SceneFit=SceneFit,
            _PIGEON_EXT=_PIGEON_EXT,
            _acquire_view1_canvas=_acquire_view1_canvas,
            _active_tmdb_logo_widget=_active_tmdb_logo_widget,
            _apply_auto_widget_policy=_apply_auto_widget_policy,
            _apply_brightness=_apply_brightness,
            _backdrop_active_for_view=_backdrop_active_for_view,
            _blend_info_cluster_into_target=_blend_info_cluster_into_target,
            _blend_top_gradient_fast=_blend_top_gradient_fast,
            _blit_saver_layers_design=_blit_saver_layers_design,
            _blit_saver_layers_target=_blit_saver_layers_target,
            _clock_saver_backdrop_brightness=_clock_saver_backdrop_brightness,
            _clock_saver_dim_overlay_bgra=_clock_saver_dim_overlay_bgra,
            _clock_saver_dim_pre_digit_canvas=_clock_saver_dim_pre_digit_canvas,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _clock_saver_layer_opacity=_clock_saver_layer_opacity,
            _clock_saver_layers=_clock_saver_layers,
            _clock_startup_intro_opacity=_clock_startup_intro_opacity,
            _compose_paused_screen=_compose_paused_screen,
            _composite_cap_dims=_composite_cap_dims,
            _composite_settings_on_canvas=_composite_settings_on_canvas,
            _design_rect_to_target=_design_rect_to_target,
            _disp_fit=_disp_fit,
            _effective_display_view=_effective_display_view,
            _hitch_parts=_hitch_parts,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _info_cluster_compose_active=_info_cluster_compose_active,
            _location_toast_alpha=_late(lambda: _location_toast_alpha, "_location_toast_alpha"),
            _paused_screen_active=_paused_screen_active,
            _playback_overlay_fast_sig=_playback_overlay_fast_sig,
            _present_frame_to_display=_present_frame_to_display,
            _refresh_clock_patch_bgra=_refresh_clock_patch_bgra,
            _set_playback_overlay_clock_saver_volume_flag=_set_playback_overlay_clock_saver_volume_flag,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _splash_reveal_clock=_splash_reveal_clock,
            _sync_now_playing_screen_state_for_frame=_sync_now_playing_screen_state_for_frame,
            _view_one_is_pigeon_poster=_view_one_is_pigeon_poster,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _view_one_video_content_a_tt_contain_rect_design=_view_one_video_content_a_tt_contain_rect_design,
            _vv_is_music=_vv_is_music,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            _warm_tmdb_logo_patch=_warm_tmdb_logo_patch,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            alpha_blend_bgra_over_bgr=alpha_blend_bgra_over_bgr,
            clock_patch_bgra=clock_patch_bgra,
            clock_saver_composite_bgra=clock_saver_composite_bgra,
            clock_widget=clock_widget,
            dev_phase=dev_phase,
            display_dims=display_dims,
            location_toast_patch_bgra=location_toast_patch_bgra,
            location_toast_state=location_toast_state,
            main_settings_widget=main_settings_widget,
            playback_lower_gradient_bgra=playback_lower_gradient_bgra,
            playback_overlay_blits=playback_overlay_blits,
            playback_overlay_flags=playback_overlay_flags,
            playback_overlay_widget=playback_overlay_widget,
            startup_ph=startup_ph,
            status_bar_blits=status_bar_blits,
            status_bar_widget=status_bar_widget,
            tmdb_tt_gradient_bgr_holder=tmdb_tt_gradient_bgr_holder,
            view_circles_widget=view_circles_widget,
        )

        compose_display_from_source = _bind_deps(
            _core_stage_render.compose_display_from_source,
            CLOCK_ANCHOR_COL=CLOCK_ANCHOR_COL,
            CLOCK_ANCHOR_ROW=CLOCK_ANCHOR_ROW,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            PATCH_LAYER_RECEIVER_AUDIO=PATCH_LAYER_RECEIVER_AUDIO,
            SceneFit=SceneFit,
            _PIGEON_EXT=_PIGEON_EXT,
            _active_tmdb_logo_widget=_active_tmdb_logo_widget,
            _active_tmdb_poster_bgra=_active_tmdb_poster_bgra,
            _apply_auto_widget_policy=_apply_auto_widget_policy,
            _apply_brightness=_apply_brightness,
            _blend_info_cluster_into_target=_blend_info_cluster_into_target,
            _blend_top_gradient_design=_blend_top_gradient_design,
            _blit_saver_layers_design=_blit_saver_layers_design,
            _clock_saver_backdrop_brightness=_clock_saver_backdrop_brightness,
            _clock_saver_dim_overlay_bgra=_clock_saver_dim_overlay_bgra,
            _clock_saver_dim_pre_digit_canvas=_clock_saver_dim_pre_digit_canvas,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _clock_saver_layer_opacity=_clock_saver_layer_opacity,
            _clock_saver_layers=_clock_saver_layers,
            _clock_startup_intro_opacity=_clock_startup_intro_opacity,
            _compose_paused_screen=_compose_paused_screen,
            _composite_cap_dims=_composite_cap_dims,
            _composite_settings_on_canvas=_composite_settings_on_canvas,
            _effective_display_view=_effective_display_view,
            _hitch_parts=_hitch_parts,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _info_cluster_compose_active=_info_cluster_compose_active,
            _location_toast_alpha=_late(lambda: _location_toast_alpha, "_location_toast_alpha"),
            _maybe_exit_settings_menus_on_idle=_maybe_exit_settings_menus_on_idle,
            _paused_screen_active=_paused_screen_active,
            _present_frame_to_display=_present_frame_to_display,
            _set_playback_overlay_clock_saver_volume_flag=_set_playback_overlay_clock_saver_volume_flag,
            _settings_is_native_1280=_settings_is_native_1280,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _splash_reveal_clock=_splash_reveal_clock,
            _stage_grid_overlay_mode=_stage_grid_overlay_mode,
            _styled_video_content_c_poster=_styled_video_content_c_poster,
            _sync_now_playing_screen_state_for_frame=_sync_now_playing_screen_state_for_frame,
            _view_one_is_pigeon_poster=_view_one_is_pigeon_poster,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _view_one_video_content_a_tt_contain_rect_design=_view_one_video_content_a_tt_contain_rect_design,
            _vv_is_music=_vv_is_music,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            alpha_blend_bgra_over_bgr=alpha_blend_bgra_over_bgr,
            blend_overlay_bgr=blend_overlay_bgr,
            build_stage_overlay_source_bgra=build_stage_overlay_source_bgra,
            clock_widget=clock_widget,
            dev_phase=dev_phase,
            display_dims=display_dims,
            get_grid_geometry=get_grid_geometry,
            location_toast_patch_bgra=location_toast_patch_bgra,
            location_toast_state=location_toast_state,
            main_settings_widget=main_settings_widget,
            playback_lower_gradient_bgra=playback_lower_gradient_bgra,
            playback_overlay_flags=playback_overlay_flags,
            playback_overlay_widget=playback_overlay_widget,
            scale_cover_center_crop=scale_cover_center_crop,
            scale_height_and_center_crop=scale_height_and_center_crop,
            startup_ph=startup_ph,
            status_bar_widget=status_bar_widget,
            tmdb_tt_gradient_bgr_holder=tmdb_tt_gradient_bgr_holder,
            view_circles_widget=view_circles_widget,
        )

        _view_four_text_is_placeholder = _core_view_four._view_four_text_is_placeholder

        _view_four_has_value = _bind_deps(
            _core_view_four._view_four_has_value,
            _view_four_has_value=_late(lambda: _view_four_has_value, "_view_four_has_value"),
            _view_four_text_is_placeholder=_view_four_text_is_placeholder,
        )

        _view_four_display_metadata = _bind_deps(
            _core_view_four._view_four_display_metadata,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _collect_view_four_raw_title_lines = _bind_deps(
            _core_view_four._collect_view_four_raw_title_lines,
            _tmdb_info_current_and_available=_tmdb_info_current_and_available,
            _view_four_display_metadata=_view_four_display_metadata,
            _view_four_has_value=_view_four_has_value,
            _view_four_text_is_placeholder=_view_four_text_is_placeholder,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            receiver_telnet_debug_holder=receiver_telnet_debug_holder,
            streaming_badge_state=streaming_badge_state,
        )

        _metadata_debug_provider = _bind_deps(
            _core_now_playing._metadata_debug_provider,
            _content_indicator_ok=_late(lambda: _content_indicator_ok, "_content_indicator_ok"),
            _tmdb_info_current_and_available=_tmdb_info_current_and_available,
            _view_four_display_metadata=_view_four_display_metadata,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            streaming_badge_state=streaming_badge_state,
        )

        try:
            from pigeon.widgets.metadata_debug import set_metadata_debug_data_provider

            set_metadata_debug_data_provider(_metadata_debug_provider)
        except Exception:
            pass

        _collect_view_four_source_lines = _bind_deps(
            _core_view_four._collect_view_four_source_lines,
            _view_four_display_metadata=_view_four_display_metadata,
            _view_four_has_value=_view_four_has_value,
            _view_four_text_is_placeholder=_view_four_text_is_placeholder,
            receiver_overlay_state=receiver_overlay_state,
        )

        _collect_view_four_playback_lines = _bind_deps(
            _core_view_four._collect_view_four_playback_lines,
            _view_four_display_metadata=_view_four_display_metadata,
            _view_four_has_value=_view_four_has_value,
            _view_four_text_is_placeholder=_view_four_text_is_placeholder,
            cap=cap,
            display_dims=display_dims,
            frame_interval_ms=frame_interval_ms,
            receiver_overlay_state=receiver_overlay_state,
        )

        _blend_view_four_debug = _bind_deps(
            _core_view_four._blend_view_four_debug,
            DisplayView=DisplayView,
            _collect_view_four_playback_lines=_collect_view_four_playback_lines,
            _collect_view_four_raw_title_lines=_collect_view_four_raw_title_lines,
            _collect_view_four_source_lines=_collect_view_four_source_lines,
            _effective_display_view=_effective_display_view,
            view_four_subview_holder=view_four_subview_holder,
        )

        _compose_shown_frame = _bind_deps(
            _core_stage_render._compose_shown_frame,
            DESIGN_H=DESIGN_H,
            DESIGN_W=DESIGN_W,
            DisplayView=DisplayView,
            SceneFit=SceneFit,
            ViewOneVariant=ViewOneVariant,
            _PIGEON_EXT=_PIGEON_EXT,
            _PROJECT_DIR=_PROJECT_DIR,
            _app_logo_clock_saver_style_now=_app_logo_clock_saver_style_now,
            _apply_brightness=_apply_brightness,
            _backdrop_active_for_view=_backdrop_active_for_view,
            _black_screen_bgr=_black_screen_bgr,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _composite_cap_dims=_composite_cap_dims,
            _current_app_display_name=_current_app_display_name,
            _current_view_one_variant=_current_view_one_variant,
            _design_grid_overlay_active=_design_grid_overlay_active,
            _effective_display_view=_effective_display_view,
            _paste_bgra_contain_on_design=_paste_bgra_contain_on_design,
            _playback_display_title=_playback_display_title,
            _present_frame_to_display=_present_frame_to_display,
            _resolve_streaming_app_logo_bgra=_resolve_streaming_app_logo_bgra,
            _view_one_is_pigeon_poster=_view_one_is_pigeon_poster,
            _view_one_variant_uses_full_path=_view_one_variant_uses_full_path,
            _view_one_variant_uses_simple_path=_view_one_variant_uses_simple_path,
            _view_one_video_content_a_tt_contain_rect_design=_view_one_video_content_a_tt_contain_rect_design,
            _vv_has_content_title=_vv_has_content_title,
            _vv_has_tmdb_bd=_vv_has_tmdb_bd,
            _vv_is_music=_vv_is_music,
            _vv_music_text_lines=_vv_music_text_lines,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            compose_display_fast_no_grid=compose_display_fast_no_grid,
            compose_display_from_source=compose_display_from_source,
            display_dims=display_dims,
            load_pigeon_temp_logo_bgra=load_pigeon_temp_logo_bgra,
            render_ui_music_text_patch_bgra=render_ui_music_text_patch_bgra,
            render_ui_text_patch_bgra=render_ui_text_patch_bgra,
            status_bar_widget=status_bar_widget,
            view_circles_widget=view_circles_widget,
        )

        if _PIGEON_EXT:
            _warm_status_bar_blits()
            _warm_playback_overlay_blits()

        # Display off: no landing art (black / stage composite only in render_once).
        if not scene_enabled[0]:
            last_frame[0] = None
            scaled_display[0] = None
            scaled_version[0] = 0

        command_entry_visible = [False]
        command_bar = tk.Frame(shell, bg="#1a1a1e", height=30)
        command_bar.pack_propagate(False)
        command_entry = tk.Entry(
            command_bar,
            bg="#2d2d32",
            fg="#f0f0f0",
            insertbackground="#f0f0f0",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#0a84ff",
            font=("Helvetica", 12),
        )
        command_entry.pack(fill=tk.BOTH, expand=True, padx=4, pady=3)

        _ui_scale = _bind_deps(
            _core_stage_render._ui_scale,
            WINDOW_H=WINDOW_H,
            WINDOW_W=WINDOW_W,
            display_dims=display_dims,
        )

        _layout_chrome = _bind_deps(
            _core_stage_render._layout_chrome,
            _ui_scale=_ui_scale,
            command_entry_visible=command_entry_visible,
            display_dims=display_dims,
            place_command_bar=_late(lambda: place_command_bar, "place_command_bar"),
        )

        place_command_bar = _bind_deps(
            _core_stage_render.place_command_bar,
            _ui_scale=_ui_scale,
            command_bar=command_bar,
            display_dims=display_dims,
        )

        hide_command_entry = _bind_deps(
            _core_tmdb_flow.hide_command_entry,
            command_bar=command_bar,
            command_entry_visible=command_entry_visible,
            label=label,
        )

        _apply_dev_phase_widgets = _bind_deps(
            _core_stage_render._apply_dev_phase_widgets,
            label=label,
            settings_frame=settings_frame,
        )

        _current_location_display_name = _core_settings_ui._current_location_display_name

        _location_toast_alpha = _bind_deps(
            _core_stage_render._location_toast_alpha,
            LOCATION_TOAST_FADE_S=LOCATION_TOAST_FADE_S,
            LOCATION_TOAST_FULL_S=LOCATION_TOAST_FULL_S,
            location_toast_state=location_toast_state,
        )

        _start_location_toast = _bind_deps(
            _core_settings_ui._start_location_toast,
            _PIGEON_EXT=_PIGEON_EXT,
            _current_location_display_name=_current_location_display_name,
            location_toast_state=location_toast_state,
            skip_cache=skip_cache,
        )

        sync_developer_chrome = _bind_deps(
            _core_stage_render.sync_developer_chrome,
            DevPhase=DevPhase,
            _PIGEON_EXT=_PIGEON_EXT,
            _apply_dev_phase_widgets=_apply_dev_phase_widgets,
            _layout_chrome=_layout_chrome,
            _settings_unbind_wheel_globals=_settings_unbind_wheel_globals,
            _start_location_toast=_start_location_toast,
            command_bar=command_bar,
            command_entry_visible=command_entry_visible,
            dev_phase=dev_phase,
            hide_command_entry=hide_command_entry,
            label=label,
            place_command_bar=place_command_bar,
            prev_dev_phase_for_location_toast=prev_dev_phase_for_location_toast,
            root=root,
        )

        toggle_play = _bind_deps(
            _core_stage_render.toggle_play,
            LANDING_DIM_BRIGHTNESS=LANDING_DIM_BRIGHTNESS,
            LANDING_DISPLAY_BRIGHTNESS=LANDING_DISPLAY_BRIGHTNESS,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            brightness_current=brightness_current,
            brightness_duration_down_s=brightness_duration_down_s,
            brightness_duration_s=brightness_duration_s,
            brightness_duration_up_s=brightness_duration_up_s,
            brightness_from=brightness_from,
            brightness_t0=brightness_t0,
            brightness_target=brightness_target,
            last_frame=last_frame,
            playing=playing,
            scene_enabled=scene_enabled,
            use_backdrop_scene=use_backdrop_scene,
        )

        quit_app = _bind_deps(_core_input_keys.quit_app, root=root)

        cycle_dev_phase = _bind_deps(
            _core_stage_render.cycle_dev_phase,
            DevPhase=DevPhase,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
        )

        _open_landing_scene = _bind_deps(
            _core_stage_render._open_landing_scene,
            _PIGEON_EXT=_PIGEON_EXT,
            _default_render_fps=_default_render_fps,
            _disp_fit=_disp_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            frame_interval_ms=frame_interval_ms,
            landing_scene_design_bgr=landing_scene_design_bgr,
            last_frame=last_frame,
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            use_backdrop_scene=use_backdrop_scene,
        )

        toggle_scene = _bind_deps(
            _core_stage_render.toggle_scene,
            _PIGEON_EXT=_PIGEON_EXT,
            _apply_brightness=_apply_brightness,
            _bgr_to_tk_image=_bgr_to_tk_image,
            _black_screen_bgr=_black_screen_bgr,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _compose_shown_frame=_compose_shown_frame,
            _design_grid_overlay_active=_design_grid_overlay_active,
            _open_landing_scene=_open_landing_scene,
            _save_persisted_scene_enabled=_save_persisted_scene_enabled,
            _update_label_photo_from_bgr=_update_label_photo_from_bgr,
            backdrop_master_bgr=backdrop_master_bgr,
            black_photo=black_photo,
            brightness_current=brightness_current,
            label=label,
            label_live_photo=label_live_photo,
            last_frame=last_frame,
            playing=playing,
            scaled_display=scaled_display,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            use_backdrop_scene=use_backdrop_scene,
        )

        _last_overlay_mono = [0.0]
        _last_s_mono = [0.0]
        _last_f10_mono = [0.0]
        _last_tmdb_hotkey_mono = [0.0]
        _last_tmdb_quality_report_mono = [0.0]
        # Set True by ⌘⇧X / Ctrl+Shift+X; failure count bumps immediately, cleared when a
        # successful TMDb populate is scored for a new content event key.
        tmdb_quality_error_flag: list[bool] = [False]
        tmdb_quality_flag_set_mono: list[float] = [0.0]
        tmdb_quality_auto_unlog_after_id: list[str | None] = [None]
        tmdb_error_flag_retry_rule_idx: list[int] = [0]
        # When True, finish_tmdb auto-advances through one full error-flag rule cycle.
        tmdb_error_flag_retry_active: list[bool] = [False]
        TMDB_QUALITY_UNLOG_WINDOW_S = 20.0
        TMDB_ERROR_FLAG_RETRY_RULES: list[tuple[str, str, str]] = [
            ("tv", "raw_title", "tv+raw_title"),
            ("tv", "alternate", "tv+alternate_query"),
            ("auto", "raw_title", "auto+raw_title"),
            ("auto", "alternate", "auto+alternate_query"),
            ("movie", "raw_title", "movie+raw_title"),
        ]
        # Last content event key that has already been scored for TMDb quality.
        tmdb_quality_last_scored_event_key: list[str] = [""]
        tmdb_quality_overlay_mode: list[str] = [""]
        tmdb_quality_overlay_t0: list[float] = [0.0]
        _last_tmdb_match_toggle_mono = [0.0]
        _last_space_mono = [0.0]
        _last_adv_shift_tab_mono = [0.0]

        _trigger_tmdb_quality_toggle_overlay = _bind_deps(
            _core_tmdb_flow._trigger_tmdb_quality_toggle_overlay,
            tmdb_quality_overlay_mode=tmdb_quality_overlay_mode,
            tmdb_quality_overlay_t0=tmdb_quality_overlay_t0,
        )

        _tmdb_quality_toggle_overlay_state = _bind_deps(
            _core_tmdb_flow._tmdb_quality_toggle_overlay_state,
            tmdb_quality_overlay_mode=tmdb_quality_overlay_mode,
            tmdb_quality_overlay_t0=tmdb_quality_overlay_t0,
        )

        _blend_tmdb_quality_toggle_overlay = _core_tmdb_flow._blend_tmdb_quality_toggle_overlay

        _blend_tmdb_quality_flag_badge = _core_tmdb_flow._blend_tmdb_quality_flag_badge

        try_cycle_dev_phase = _bind_deps(
            _core_input_keys.try_cycle_dev_phase,
            _last_overlay_mono=_last_overlay_mono,
            cycle_dev_phase=cycle_dev_phase,
        )

        on_tab_key = _bind_deps(
            _core_input_keys.on_tab_key,
            _last_overlay_mono=_last_overlay_mono,
            cycle_dev_phase=cycle_dev_phase,
        )

        on_shift_tab_dev_cycle = _bind_deps(
            _core_input_keys.on_shift_tab_dev_cycle,
            on_tab_key=on_tab_key,
        )

        on_ctrl_tab = _bind_deps(_core_input_keys.on_ctrl_tab, on_tab_key=on_tab_key)

        on_s_key = _bind_deps(
            _core_input_keys.on_s_key,
            _design_grid_overlay_active=_design_grid_overlay_active,
            _last_s_mono=_last_s_mono,
            toggle_scene=toggle_scene,
        )

        apply_saved_tmdb_backdrop_to_display = _bind_deps(
            _core_tmdb_flow.apply_saved_tmdb_backdrop_to_display,
            BACKDROP_BRIGHTNESS=BACKDROP_BRIGHTNESS,
            _PIGEON_EXT=_PIGEON_EXT,
            _app_logo_clock_saver_style_now=_app_logo_clock_saver_style_now,
            _apply_netflix_backdrop_when_running=_apply_netflix_backdrop_when_running,
            _save_persisted_scene_enabled=_save_persisted_scene_enabled,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _warm_status_bar_blits=_warm_status_bar_blits,
            _warm_tmdb_logo_patch=_warm_tmdb_logo_patch,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            brightness_current=brightness_current,
            brightness_from=brightness_from,
            brightness_t0=brightness_t0,
            brightness_target=brightness_target,
            display_dims=display_dims,
            last_frame=last_frame,
            playing=playing,
            render_once=_late(lambda: render_once, "render_once"),
            saved_backdrop_app_logo_letterbox_fit=saved_backdrop_app_logo_letterbox_fit,
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
            use_backdrop_scene=use_backdrop_scene,
            view_circles_widget=view_circles_widget,
        )

        f10_cycle_scene_grid = _bind_deps(
            _core_stage_render.f10_cycle_scene_grid,
            LANDING_DISPLAY_BRIGHTNESS=LANDING_DISPLAY_BRIGHTNESS,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _open_landing_scene=_open_landing_scene,
            _save_persisted_scene_enabled=_save_persisted_scene_enabled,
            apply_saved_tmdb_backdrop_to_display=apply_saved_tmdb_backdrop_to_display,
            backdrop_master_bgr=backdrop_master_bgr,
            brightness_current=brightness_current,
            brightness_from=brightness_from,
            brightness_t0=brightness_t0,
            brightness_target=brightness_target,
            last_frame=last_frame,
            playing=playing,
            render_once=_late(lambda: render_once, "render_once"),
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
            scaled_display=scaled_display,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            use_backdrop_scene=use_backdrop_scene,
        )

        on_f10_key = _bind_deps(
            _core_input_keys.on_f10_key,
            _design_grid_overlay_active=_design_grid_overlay_active,
            _last_f10_mono=_last_f10_mono,
            f10_cycle_scene_grid=f10_cycle_scene_grid,
            toggle_scene=toggle_scene,
        )

        on_click_focus = _bind_deps(
            _core_input_keys.on_click_focus,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            label=label,
            root=root,
        )

        on_double_click_scene = _bind_deps(
            _core_input_keys.on_double_click_scene,
            toggle_scene=toggle_scene,
        )

        show_command_entry = _bind_deps(
            _core_tmdb_flow.show_command_entry,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            command_bar=command_bar,
            command_entry=command_entry,
            command_entry_visible=command_entry_visible,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
            place_command_bar=place_command_bar,
            root=root,
        )

        _last_command_submit_mono = [0.0]

        parse_tmdb_command_phrase = _core_tmdb_flow.parse_tmdb_command_phrase

        _escape_log_field = _core_tmdb_flow._escape_log_field

        _append_tmdb_quality_event_report_log = _bind_deps(
            _core_tmdb_flow._append_tmdb_quality_event_report_log,
            _escape_log_field=_escape_log_field,
            apple_tv_auto_state=apple_tv_auto_state,
            streaming_badge_state=streaming_badge_state,
        )

        _clear_displayed_tmdb_art_for_content_change = _bind_deps(
            _core_tmdb_flow._clear_displayed_tmdb_art_for_content_change,
            _clear_now_playing_view_caches=_clear_now_playing_view_caches,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _tmdb_poster_cache=_tmdb_poster_cache,
            _tmdb_tt_src_cache=_tmdb_tt_src_cache,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _warm_tmdb_logo_patch=_warm_tmdb_logo_patch,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            backdrop_master_bgr=backdrop_master_bgr,
            skip_cache=skip_cache,
            tmdb_error_flag_retry_active=tmdb_error_flag_retry_active,
            tmdb_error_flag_retry_rule_idx=tmdb_error_flag_retry_rule_idx,
            tmdb_logo_app_fallback_active=tmdb_logo_app_fallback_active,
            tmdb_logo_patch_bgra=tmdb_logo_patch_bgra,
            tmdb_logo_widget=tmdb_logo_widget,
            tmdb_logo_widget_view_six=tmdb_logo_widget_view_six,
        )

        _mark_tmdb_missing_art = _bind_deps(
            _core_tmdb_flow._mark_tmdb_missing_art,
            apple_tv_auto_state=apple_tv_auto_state,
            tmdb_error_flag_retry_active=tmdb_error_flag_retry_active,
        )

        _clear_tmdb_missing_art = _bind_deps(
            _core_tmdb_flow._clear_tmdb_missing_art,
            apple_tv_auto_state=apple_tv_auto_state,
            tmdb_error_flag_retry_active=tmdb_error_flag_retry_active,
        )

        spawn_tmdb_poster_fetch = _bind_deps(
            _core_tmdb_flow.spawn_tmdb_poster_fetch,
            BACKDROP_BRIGHTNESS=BACKDROP_BRIGHTNESS,
            TMDB_ERROR_FLAG_RETRY_RULES=TMDB_ERROR_FLAG_RETRY_RULES,
            _PIGEON_EXT=_PIGEON_EXT,
            _append_tmdb_quality_event_report_log=_append_tmdb_quality_event_report_log,
            _apply_netflix_backdrop_when_running=_apply_netflix_backdrop_when_running,
            _apply_rawtitle_text_tt_fallback=_late(lambda: _apply_rawtitle_text_tt_fallback, "_apply_rawtitle_text_tt_fallback"),
            _backdrop_master_from_streaming_app_logo=_backdrop_master_from_streaming_app_logo,
            _cancel_tmdb_quality_auto_unlog_timer=_late(lambda: _cancel_tmdb_quality_auto_unlog_timer, "_cancel_tmdb_quality_auto_unlog_timer"),
            _clear_now_playing_view_caches=_clear_now_playing_view_caches,
            _clear_tmdb_missing_art=_clear_tmdb_missing_art,
            _mark_tmdb_missing_art=_mark_tmdb_missing_art,
            _perform_tmdb_error_flag_retry=_late(lambda: _perform_tmdb_error_flag_retry, "_perform_tmdb_error_flag_retry"),
            _save_persisted_scene_enabled=_save_persisted_scene_enabled,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _tmdb_match_tier_acceptable=_late(lambda: _tmdb_match_tier_acceptable, "_tmdb_match_tier_acceptable"),
            _tmdb_spawn_identity=_late(lambda: _tmdb_spawn_identity, "_tmdb_spawn_identity"),
            _tmdb_spawn_identity_changed=_late(lambda: _tmdb_spawn_identity_changed, "_tmdb_spawn_identity_changed"),
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _vv_is_music=_vv_is_music,
            _vv_is_youtube=_vv_is_youtube,
            _warm_status_bar_blits=_warm_status_bar_blits,
            _warm_tmdb_logo_patch=_warm_tmdb_logo_patch,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            brightness_current=brightness_current,
            brightness_from=brightness_from,
            brightness_t0=brightness_t0,
            brightness_target=brightness_target,
            cap=cap,
            last_frame=last_frame,
            playing=playing,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            saved_backdrop_app_logo_letterbox_fit=saved_backdrop_app_logo_letterbox_fit,
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            spawn_tmdb_poster_fetch=_late(lambda: spawn_tmdb_poster_fetch, "spawn_tmdb_poster_fetch"),
            status_bar_widget=status_bar_widget,
            streaming_badge_state=streaming_badge_state,
            tmdb_error_flag_retry_active=tmdb_error_flag_retry_active,
            tmdb_error_flag_retry_rule_idx=tmdb_error_flag_retry_rule_idx,
            tmdb_logo_app_fallback_active=tmdb_logo_app_fallback_active,
            tmdb_logo_widget=tmdb_logo_widget,
            tmdb_logo_widget_view_six=tmdb_logo_widget_view_six,
            tmdb_quality_error_flag=tmdb_quality_error_flag,
            tmdb_quality_last_scored_event_key=tmdb_quality_last_scored_event_key,
            use_backdrop_scene=use_backdrop_scene,
        )

        _content_indicator_ok = _bind_deps(
            _core_settings_ui._content_indicator_ok,
            _PIGEON_EXT=_PIGEON_EXT,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_busy=apple_tv_busy,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            current_apple_tv=current_apple_tv,
        )

        _advanced_feature_pipeline_ok = _bind_deps(
            _core_settings_ui._advanced_feature_pipeline_ok,
            _PIGEON_EXT=_PIGEON_EXT,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_busy=apple_tv_busy,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            current_apple_tv=current_apple_tv,
        )

        _read_tmdb_quality_counts = _bind_deps(
            _core_tmdb_flow._read_tmdb_quality_counts,
            _PIGEON_EXT=_PIGEON_EXT,
        )

        _cancel_tmdb_quality_auto_unlog_timer = _bind_deps(
            _core_tmdb_flow._cancel_tmdb_quality_auto_unlog_timer,
            root=root,
            tmdb_quality_auto_unlog_after_id=tmdb_quality_auto_unlog_after_id,
        )

        _schedule_tmdb_quality_auto_expire = _bind_deps(
            _core_tmdb_flow._schedule_tmdb_quality_auto_expire,
            TMDB_QUALITY_UNLOG_WINDOW_S=TMDB_QUALITY_UNLOG_WINDOW_S,
            _cancel_tmdb_quality_auto_unlog_timer=_cancel_tmdb_quality_auto_unlog_timer,
            root=root,
            skip_cache=skip_cache,
            tmdb_quality_auto_unlog_after_id=tmdb_quality_auto_unlog_after_id,
            tmdb_quality_error_flag=tmdb_quality_error_flag,
        )

        _apply_rawtitle_text_tt_fallback = _bind_deps(
            _core_tmdb_flow._apply_rawtitle_text_tt_fallback,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            tmdb_logo_app_fallback_active=tmdb_logo_app_fallback_active,
        )

        _tmdb_match_tier_acceptable = _core_tmdb_flow._tmdb_match_tier_acceptable

        _format_tmdb_match_quality_glance = _core_tmdb_flow._format_tmdb_match_quality_glance

        _refresh_match_quality_glance_label = _core_tmdb_flow._refresh_match_quality_glance_label

        _adjust_tmdb_quality_failure_delta = _bind_deps(
            _core_tmdb_flow._adjust_tmdb_quality_failure_delta,
            _PIGEON_EXT=_PIGEON_EXT,
            _refresh_match_quality_glance_label=_refresh_match_quality_glance_label,
            match_quality_glance_sig=match_quality_glance_sig,
        )

        _clear_tmdb_quality_flag = _bind_deps(
            _core_tmdb_flow._clear_tmdb_quality_flag,
            _adjust_tmdb_quality_failure_delta=_adjust_tmdb_quality_failure_delta,
            _cancel_tmdb_quality_auto_unlog_timer=_cancel_tmdb_quality_auto_unlog_timer,
            _trigger_tmdb_quality_toggle_overlay=_trigger_tmdb_quality_toggle_overlay,
            skip_cache=skip_cache,
            tmdb_quality_error_flag=tmdb_quality_error_flag,
        )

        on_reset_tmdb_match_quality_stats = _bind_deps(
            _core_tmdb_flow.on_reset_tmdb_match_quality_stats,
            _PIGEON_EXT=_PIGEON_EXT,
            _refresh_match_quality_glance_label=_refresh_match_quality_glance_label,
            match_quality_glance_sig=match_quality_glance_sig,
        )

        _refresh_content_indicator = _bind_deps(
            _core_settings_ui._refresh_content_indicator,
            _content_indicator_ok=_content_indicator_ok,
            _paint_boolean_led=_paint_boolean_led,
            _refresh_match_quality_glance_label=_refresh_match_quality_glance_label,
            content_indicator_cv_holder=content_indicator_cv_holder,
        )

        _paint_pair_led = _bind_deps(
            _core_settings_ui._paint_pair_led,
            pairing_led_holder=pairing_led_holder,
        )

        _paint_cred_led_canvas = _core_settings_ui._paint_cred_led_canvas

        _remove_streaming_device_at = _bind_deps(
            _core_pairing._remove_streaming_device_at,
            _clear_reported_position_stall_stamp=_clear_reported_position_stall_stamp,
            _rebuild_paired_devices_panel=_late(lambda: _rebuild_paired_devices_panel, "_rebuild_paired_devices_panel"),
            _schedule_refresh_pairing_leds=_late(lambda: _schedule_refresh_pairing_leds, "_schedule_refresh_pairing_leds"),
            _sync_status_bar_visibility_for_playback=_late(lambda: _sync_status_bar_visibility_for_playback, "_sync_status_bar_visibility_for_playback"),
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_busy=apple_tv_busy,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            apple_tv_playback_clock=apple_tv_playback_clock,
            current_apple_tv=current_apple_tv,
            describe_current_apple_tv=_late(lambda: describe_current_apple_tv, "describe_current_apple_tv"),
            playback_overlay_widget=playback_overlay_widget,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            skip_cache=skip_cache,
            streaming_slot_holder=streaming_slot_holder,
        )

        _remove_receiver_device_at = _bind_deps(
            _core_pairing._remove_receiver_device_at,
            _rebuild_paired_devices_panel=_late(lambda: _rebuild_paired_devices_panel, "_rebuild_paired_devices_panel"),
            _schedule_refresh_pairing_leds=_late(lambda: _schedule_refresh_pairing_leds, "_schedule_refresh_pairing_leds"),
            apple_tv_busy=apple_tv_busy,
            avr_slot_holder=avr_slot_holder,
            describe_current_apple_tv=_late(lambda: describe_current_apple_tv, "describe_current_apple_tv"),
            playback_overlay_widget=playback_overlay_widget,
            receiver_http_host=receiver_http_host,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            skip_cache=skip_cache,
        )

        _paired_box_close_button = _bind_deps(
            _core_settings_ui._paired_box_close_button,
            _S=_S,
            _kiosk_on=_kiosk_on,
        )

        _settings_parse_device_rows = _core_settings_ui._settings_parse_device_rows

        _settings_parse_receiver_rows = _core_settings_ui._settings_parse_receiver_rows

        _remove_aux_slot_device_at = _bind_deps(
            _core_pairing._remove_aux_slot_device_at,
            _apply_persisted_location_to_runtime=_late(lambda: _apply_persisted_location_to_runtime, "_apply_persisted_location_to_runtime"),
            _rebuild_paired_devices_panel=_late(lambda: _rebuild_paired_devices_panel, "_rebuild_paired_devices_panel"),
            _schedule_refresh_pairing_leds=_late(lambda: _schedule_refresh_pairing_leds, "_schedule_refresh_pairing_leds"),
            apple_tv_busy=apple_tv_busy,
            describe_current_apple_tv=_late(lambda: describe_current_apple_tv, "describe_current_apple_tv"),
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            skip_cache=skip_cache,
        )

        _rebuild_paired_devices_panel = _bind_deps(
            _core_settings_ui._rebuild_paired_devices_panel,
            S_FONT_CAP_BOLD=S_FONT_CAP_BOLD,
            S_FONT_SEC=S_FONT_SEC,
            S_FONT_SMALL=S_FONT_SMALL,
            _paint_boolean_led=_paint_boolean_led,
            _paint_cred_led_canvas=_paint_cred_led_canvas,
            _paired_box_close_button=_paired_box_close_button,
            _remove_aux_slot_device_at=_remove_aux_slot_device_at,
            _remove_receiver_device_at=_remove_receiver_device_at,
            _remove_streaming_device_at=_remove_streaming_device_at,
            _settings_parse_device_rows=_settings_parse_device_rows,
            _settings_parse_receiver_rows=_settings_parse_receiver_rows,
            paired_devices_inner=paired_devices_inner,
            paired_observed_led_last_state=paired_observed_led_last_state,
            paired_observed_led_rows=paired_observed_led_rows,
            paired_ui_leds=paired_ui_leds,
            receiver_panel_led_holder=receiver_panel_led_holder,
        )

        _seed_current_apple_tv_from_streaming_slot = _bind_deps(
            _core_device_control._seed_current_apple_tv_from_streaming_slot,
            current_apple_tv=current_apple_tv,
            streaming_slot_holder=streaming_slot_holder,
        )

        describe_current_apple_tv = _bind_deps(
            _core_device_control.describe_current_apple_tv,
            _refresh_content_indicator=_refresh_content_indicator,
            apple_tv_status_var=apple_tv_status_var,
            current_apple_tv=current_apple_tv,
            receiver_http_host=receiver_http_host,
        )

        def set_apple_tv_controls_enabled(enabled: bool) -> None:
            state = tk.NORMAL if enabled else tk.DISABLED
            try:
                find_device_btn.configure(state=state)
                _m = tmdb_adv_manual_btn_holder[0]
                if _m is not None:
                    _m.configure(state=state)
                _r = tmdb_adv_report_btn_holder[0]
                if _r is not None:
                    _r.configure(state=state)
                purge_image_media_btn.configure(state=state)
                _fdb = settings_footer_debug_holder[0]
                if _fdb is not None:
                    _fdb.configure(state=state)
                _frb = settings_footer_reset_holder[0]
                if _frb is not None:
                    _frb.configure(state=state)
                root.configure(cursor="none")
            except tk.TclError:
                pass

        begin_apple_tv_operation = _bind_deps(
            _core_device_control.begin_apple_tv_operation,
            apple_tv_busy=apple_tv_busy,
            describe_current_apple_tv=describe_current_apple_tv,
            set_apple_tv_controls_enabled=set_apple_tv_controls_enabled,
        )

        end_apple_tv_operation = _bind_deps(
            _core_device_control.end_apple_tv_operation,
            apple_tv_busy=apple_tv_busy,
            describe_current_apple_tv=describe_current_apple_tv,
            set_apple_tv_controls_enabled=set_apple_tv_controls_enabled,
        )

        set_current_apple_tv = _bind_deps(
            _core_device_control.set_current_apple_tv,
            _atv_ix_extrap_playing=_atv_ix_extrap_playing,
            _atv_ix_pos=_atv_ix_pos,
            _atv_ix_pos_mono=_atv_ix_pos_mono,
            _atv_ix_prev_idle=_atv_ix_prev_idle,
            _atv_ix_sig_ck=_atv_ix_sig_ck,
            _atv_ix_sig_ds=_atv_ix_sig_ds,
            _clear_reported_position_stall_stamp=_clear_reported_position_stall_stamp,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _reset_clock_saver_device_signal_baseline=_reset_clock_saver_device_signal_baseline,
            _schedule_refresh_pairing_leds=_late(lambda: _schedule_refresh_pairing_leds, "_schedule_refresh_pairing_leds"),
            _sync_status_bar_visibility_for_playback=_late(lambda: _sync_status_bar_visibility_for_playback, "_sync_status_bar_visibility_for_playback"),
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            apple_tv_playback_clock=apple_tv_playback_clock,
            current_apple_tv=current_apple_tv,
            describe_current_apple_tv=describe_current_apple_tv,
            last_atv_interaction_mono=last_atv_interaction_mono,
        )

        _apply_persisted_location_to_runtime = _bind_deps(
            _core_device_control._apply_persisted_location_to_runtime,
            _atv_ix_extrap_playing=_atv_ix_extrap_playing,
            _atv_ix_pos=_atv_ix_pos,
            _atv_ix_pos_mono=_atv_ix_pos_mono,
            _atv_ix_prev_idle=_atv_ix_prev_idle,
            _atv_ix_sig_ck=_atv_ix_sig_ck,
            _atv_ix_sig_ds=_atv_ix_sig_ds,
            _clear_reported_position_stall_stamp=_clear_reported_position_stall_stamp,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _reset_clock_saver_device_signal_baseline=_reset_clock_saver_device_signal_baseline,
            _schedule_refresh_pairing_leds=_late(lambda: _schedule_refresh_pairing_leds, "_schedule_refresh_pairing_leds"),
            _start_location_toast=_start_location_toast,
            _sync_status_bar_visibility_for_playback=_late(lambda: _sync_status_bar_visibility_for_playback, "_sync_status_bar_visibility_for_playback"),
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            apple_tv_playback_clock=apple_tv_playback_clock,
            avr_slot_holder=avr_slot_holder,
            current_apple_tv=current_apple_tv,
            describe_current_apple_tv=describe_current_apple_tv,
            last_atv_interaction_mono=last_atv_interaction_mono,
            playback_overlay_widget=playback_overlay_widget,
            receiver_http_host=receiver_http_host,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            streaming_slot_holder=streaming_slot_holder,
        )

        _refresh_location_selector = _bind_deps(
            _core_settings_ui._refresh_location_selector,
            _apply_persisted_location_to_runtime=_apply_persisted_location_to_runtime,
            _refresh_location_selector=_late(lambda: _refresh_location_selector, "_refresh_location_selector"),
            delete_location_btn=delete_location_btn,
            location_menu_var=location_menu_var,
            location_name_entry=location_name_entry,
            location_name_var=location_name_var,
            location_om_frame=location_om_frame,
            location_option_holder=location_option_holder,
            rename_name_btn=rename_name_btn,
            root=root,
        )

        _on_delete_current_location = _bind_deps(
            _core_settings_ui._on_delete_current_location,
            _apply_persisted_location_to_runtime=_apply_persisted_location_to_runtime,
            _refresh_location_selector=_refresh_location_selector,
            _start_location_toast=_start_location_toast,
            root=root,
        )

        delete_location_btn.configure(command=_on_delete_current_location)

        _refresh_observed_pairing_led_rows = _bind_deps(
            _core_settings_ui._refresh_observed_pairing_led_rows,
            _paint_cred_led_canvas=_paint_cred_led_canvas,
            paired_observed_led_last_state=paired_observed_led_last_state,
            paired_observed_led_rows=paired_observed_led_rows,
        )

        _schedule_refresh_pairing_leds = _bind_deps(
            _core_pairing._schedule_refresh_pairing_leds,
            _PIGEON_EXT=_PIGEON_EXT,
            _content_indicator_ok=_content_indicator_ok,
            _paint_boolean_led=_paint_boolean_led,
            _paint_cred_led_canvas=_paint_cred_led_canvas,
            _paint_pair_led=_paint_pair_led,
            _pair_led_pending_retry=_pair_led_pending_retry,
            _refresh_observed_pairing_led_rows=_refresh_observed_pairing_led_rows,
            _schedule_refresh_pairing_leds=_late(lambda: _schedule_refresh_pairing_leds, "_schedule_refresh_pairing_leds"),
            apple_tv_busy=apple_tv_busy,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            main_settings_widget=main_settings_widget,
            pair_led_busy=pair_led_busy,
            paired_ui_leds=paired_ui_leds,
            root=root,
            streaming_row_led_canvas_holder=streaming_row_led_canvas_holder,
            streaming_slot_holder=streaming_slot_holder,
        )

        _device_addr_key = _core_settings_ui._device_addr_key

        _device_row_matches_saved = _bind_deps(
            _core_settings_ui._device_row_matches_saved,
            _device_addr_key=_device_addr_key,
        )

        _verify_added_devices_after_save = _bind_deps(
            _core_pairing._verify_added_devices_after_save,
            root=root,
        )

        describe_current_apple_tv()
        _refresh_location_selector()
        _rebuild_paired_devices_panel()

        _ask_pairing_pin_modal = _bind_deps(
            _core_settings_ui._ask_pairing_pin_modal,
            S_FONT_BODY=S_FONT_BODY,
            S_FONT_BTN=S_FONT_BTN,
            root=root,
        )

        _start_airplay_pairing_sequence = _bind_deps(
            _core_pairing._start_airplay_pairing_sequence,
            _ask_pairing_pin_modal=_ask_pairing_pin_modal,
            _pyatv_install_hint=_pyatv_install_hint,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            begin_apple_tv_operation=begin_apple_tv_operation,
            describe_current_apple_tv=describe_current_apple_tv,
            end_apple_tv_operation=end_apple_tv_operation,
            root=root,
        )

        _finish_remote_then_start_airplay = _bind_deps(
            _core_pairing._finish_remote_then_start_airplay,
            _pyatv_install_hint=_pyatv_install_hint,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            _start_airplay_pairing_sequence=_start_airplay_pairing_sequence,
            end_apple_tv_operation=end_apple_tv_operation,
            root=root,
        )

        _run_sequential_player_pairing_wizard = _bind_deps(
            _core_pairing._run_sequential_player_pairing_wizard,
            _ask_pairing_pin_modal=_ask_pairing_pin_modal,
            _finish_remote_then_start_airplay=_finish_remote_then_start_airplay,
            _pyatv_install_hint=_pyatv_install_hint,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            begin_apple_tv_operation=begin_apple_tv_operation,
            describe_current_apple_tv=describe_current_apple_tv,
            end_apple_tv_operation=end_apple_tv_operation,
            root=root,
        )

        _save_box_pair_device_row = _bind_deps(
            _core_settings_ui._save_box_pair_device_row,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            avr_slot_holder=avr_slot_holder,
            describe_current_apple_tv=describe_current_apple_tv,
            receiver_http_host=receiver_http_host,
            streaming_slot_holder=streaming_slot_holder,
        )

        _handle_main_settings_action = _bind_deps(
            _core_settings_ui._handle_main_settings_action,
            _apply_persisted_location_to_runtime=_apply_persisted_location_to_runtime,
            _pyatv_install_hint=_pyatv_install_hint,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _refresh_location_selector=_refresh_location_selector,
            _resolve_install_root_for_update=_resolve_install_root_for_update,
            _save_box_pair_device_row=_save_box_pair_device_row,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            _sync_update_button_style=_sync_update_button_style,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            avr_slot_holder=avr_slot_holder,
            begin_apple_tv_operation=begin_apple_tv_operation,
            current_apple_tv=current_apple_tv,
            describe_current_apple_tv=describe_current_apple_tv,
            discovery_scan_cache=discovery_scan_cache,
            end_apple_tv_operation=end_apple_tv_operation,
            main_settings_widget=main_settings_widget,
            pair_led_busy=pair_led_busy,
            receiver_http_host=receiver_http_host,
            root=root,
            skip_cache=skip_cache,
            streaming_slot_holder=streaming_slot_holder,
            update_check_state=update_check_state,
        )

        _force_advanced_feature_try = _bind_deps(
            _core_settings_ui._force_advanced_feature_try,
            _apple_tv_auto_poll_tick=_late(lambda: _apple_tv_auto_poll_tick, "_apple_tv_auto_poll_tick"),
            _receiver_poll_tick=_late(lambda: _receiver_poll_tick, "_receiver_poll_tick"),
            on_apple_tv_selected_then_tmdb=_late(lambda: on_apple_tv_selected_then_tmdb, "on_apple_tv_selected_then_tmdb"),
        )

        _on_advanced_matrix_closed = _bind_deps(
            _core_stage_render._on_advanced_matrix_closed,
            advanced_matrix_restore_phase=advanced_matrix_restore_phase,
            dev_phase=dev_phase,
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
        )

        _open_advanced_capability_matrix = _bind_deps(
            _core_settings_ui._open_advanced_capability_matrix,
            _PIGEON_EXT=_PIGEON_EXT,
            _advanced_feature_pipeline_ok=_advanced_feature_pipeline_ok,
            _force_advanced_feature_try=_force_advanced_feature_try,
            _on_advanced_matrix_closed=_on_advanced_matrix_closed,
            _perform_tmdb_artwork_retry=_late(lambda: _perform_tmdb_artwork_retry, "_perform_tmdb_artwork_retry"),
            _prepend_hotkey_bindtag=_prepend_hotkey_bindtag,
            _register_tmdb_adv_widgets=_register_tmdb_adv_widgets,
            _tmdb_retry_log_read_tail=_tmdb_retry_log_read_tail,
            _unregister_tmdb_adv_widgets=_unregister_tmdb_adv_widgets,
            advanced_matrix_close_skip=advanced_matrix_close_skip,
            on_apple_tv_selected_then_tmdb=_late(lambda: on_apple_tv_selected_then_tmdb, "on_apple_tv_selected_then_tmdb"),
            root=root,
        )

        _open_find_device_dialog = _bind_deps(
            _core_pairing._open_find_device_dialog,
            DISCOVERY_CACHE_TTL_S=DISCOVERY_CACHE_TTL_S,
            S_FONT_BODY=S_FONT_BODY,
            S_FONT_BTN=S_FONT_BTN,
            S_FONT_MICRO=S_FONT_MICRO,
            S_FONT_SMALL=S_FONT_SMALL,
            S_FONT_STATUS=S_FONT_STATUS,
            _LISTBOX_BG=_LISTBOX_BG,
            _LISTBOX_FG=_LISTBOX_FG,
            _PIGEON_EXT=_PIGEON_EXT,
            _apply_persisted_location_to_runtime=_apply_persisted_location_to_runtime,
            _pyatv_install_hint=_pyatv_install_hint,
            _refresh_location_selector=_refresh_location_selector,
            _run_sequential_player_pairing_wizard=_run_sequential_player_pairing_wizard,
            _verify_added_devices_after_save=_verify_added_devices_after_save,
            begin_apple_tv_operation=begin_apple_tv_operation,
            discovery_scan_cache=discovery_scan_cache,
            end_apple_tv_operation=end_apple_tv_operation,
            root=root,
        )

        on_reset_pigeon_devices_and_media = _bind_deps(
            _core_settings_ui.on_reset_pigeon_devices_and_media,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _refresh_location_selector=_refresh_location_selector,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            avr_slot_holder=avr_slot_holder,
            current_apple_tv=current_apple_tv,
            describe_current_apple_tv=describe_current_apple_tv,
            discovery_scan_cache=discovery_scan_cache,
            playback_overlay_widget=playback_overlay_widget,
            receiver_http_host=receiver_http_host,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            skip_cache=skip_cache,
            streaming_slot_holder=streaming_slot_holder,
        )

        on_apple_tv_selected_then_tmdb = _bind_deps(
            _core_device_control.on_apple_tv_selected_then_tmdb,
            _PIGEON_EXT=_PIGEON_EXT,
            _open_find_device_dialog=_open_find_device_dialog,
            _pyatv_install_hint=_pyatv_install_hint,
            apple_tv_busy=apple_tv_busy,
            begin_apple_tv_operation=begin_apple_tv_operation,
            describe_current_apple_tv=describe_current_apple_tv,
            end_apple_tv_operation=end_apple_tv_operation,
            last_atv_interaction_mono=last_atv_interaction_mono,
            root=root,
            set_current_apple_tv=set_current_apple_tv,
            spawn_tmdb_poster_fetch=spawn_tmdb_poster_fetch,
            streaming_slot_holder=streaming_slot_holder,
        )

        _content_key_from_metadata = _bind_deps(
            _core_now_playing._content_key_from_metadata,
            _tmdb_pref_from_metadata=_late(lambda: _tmdb_pref_from_metadata, "_tmdb_pref_from_metadata"),
            resolve_metadata_tmdb_query=resolve_metadata_tmdb_query,
        )

        _player_duration_for_tmdb = _bind_deps(
            _core_now_playing._player_duration_for_tmdb,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _tmdb_duration_bucket = _bind_deps(
            _core_tmdb_flow._tmdb_duration_bucket,
            _player_duration_for_tmdb=_player_duration_for_tmdb,
        )

        _tmdb_spawn_identity = _bind_deps(
            _core_tmdb_flow._tmdb_spawn_identity,
            _tmdb_duration_bucket=_tmdb_duration_bucket,
        )

        _tmdb_spawn_identity_changed = _bind_deps(
            _core_tmdb_flow._tmdb_spawn_identity_changed,
            _content_key_from_metadata=_content_key_from_metadata,
            _tmdb_spawn_identity=_tmdb_spawn_identity,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _tmdb_pref_from_metadata = _core_tmdb_flow._tmdb_pref_from_metadata

        _apply_playback_clock_from_poll = _bind_deps(
            _core_now_playing._apply_playback_clock_from_poll,
            _content_key_from_metadata=_content_key_from_metadata,
            apple_tv_playback_clock=apple_tv_playback_clock,
            last_timecode_motion_mono=last_timecode_motion_mono,
        )

        _playback_extrapolated_pair = _bind_deps(
            _core_now_playing._playback_extrapolated_pair,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _sync_trt_text_to_true_once = _bind_deps(
            _core_now_playing._sync_trt_text_to_true_once,
            _format_hmmss=_format_hmmss,
            _playback_extrapolated_pair=_playback_extrapolated_pair,
            _playback_progress_fraction_for_bar=_late(lambda: _playback_progress_fraction_for_bar, "_playback_progress_fraction_for_bar"),
            _refresh_trt_progress_only=_late(lambda: _refresh_trt_progress_only, "_refresh_trt_progress_only"),
            _sync_status_bar_trt_substantive=_late(lambda: _sync_status_bar_trt_substantive, "_sync_status_bar_trt_substantive"),
            _warm_status_bar_blits=_warm_status_bar_blits,
            apple_tv_playback_clock=apple_tv_playback_clock,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
        )

        _update_status_bar_from_metadata = _bind_deps(
            _core_now_playing._update_status_bar_from_metadata,
            _apply_playback_clock_from_poll=_apply_playback_clock_from_poll,
            _sync_trt_text_to_true_once=_sync_trt_text_to_true_once,
        )

        _playback_progress_fraction_for_bar = _bind_deps(
            _core_now_playing._playback_progress_fraction_for_bar,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _refresh_trt_progress_only = _bind_deps(
            _core_now_playing._refresh_trt_progress_only,
            _playback_progress_fraction_for_bar=_playback_progress_fraction_for_bar,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _warm_status_bar_blits=_warm_status_bar_blits,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
        )

        # Mid-bootstrap (~1–2 s in): SVG chrome is warm; full sync is safe now.
        _warm_view_one_splash_chrome_only(phase="chrome-mid-bootstrap")

        _playback_ui_tick = _bind_deps(
            _core_now_playing._playback_ui_tick,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _playback_ui_tick=_late(lambda: _playback_ui_tick, "_playback_ui_tick"),
            _refresh_content_indicator=_refresh_content_indicator,
            _refresh_extrapolated_timecodes=_late(lambda: _refresh_extrapolated_timecodes, "_refresh_extrapolated_timecodes"),
            apple_tv_playback_clock=apple_tv_playback_clock,
            root=root,
        )

        _update_atv_interaction_from_poll_metadata = _bind_deps(
            _core_device_control._update_atv_interaction_from_poll_metadata,
            _atv_ix_extrap_playing=_atv_ix_extrap_playing,
            _atv_ix_pos=_atv_ix_pos,
            _atv_ix_pos_mono=_atv_ix_pos_mono,
            _atv_ix_prev_idle=_atv_ix_prev_idle,
            _atv_ix_sig_ck=_atv_ix_sig_ck,
            _atv_ix_sig_ds=_atv_ix_sig_ds,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _content_key_from_metadata=_content_key_from_metadata,
            current_apple_tv=current_apple_tv,
            last_atv_interaction_mono=last_atv_interaction_mono,
        )

        _trt_substantive_from_clock = _bind_deps(
            _core_now_playing._trt_substantive_from_clock,
            apple_tv_playback_clock=apple_tv_playback_clock,
        )

        _trt_substantive_for_status_bar = _bind_deps(
            _core_now_playing._trt_substantive_for_status_bar,
            _trt_substantive_from_clock=_trt_substantive_from_clock,
        )

        _sync_status_bar_trt_substantive = _bind_deps(
            _core_now_playing._sync_status_bar_trt_substantive,
            _trt_substantive_for_status_bar=_trt_substantive_for_status_bar,
            _warm_status_bar_blits=_warm_status_bar_blits,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
        )

        _refresh_extrapolated_timecodes = _bind_deps(
            _core_now_playing._refresh_extrapolated_timecodes,
            _format_hmmss=_format_hmmss,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _playback_extrapolated_pair=_playback_extrapolated_pair,
            _playback_progress_fraction_for_bar=_playback_progress_fraction_for_bar,
            _sync_status_bar_trt_substantive=_sync_status_bar_trt_substantive,
            _warm_status_bar_blits=_warm_status_bar_blits,
            apple_tv_playback_clock=apple_tv_playback_clock,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
        )

        _sync_status_bar_visibility_for_playback = _bind_deps(
            _core_now_playing._sync_status_bar_visibility_for_playback,
            DisplayView=DisplayView,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _effective_display_view=_effective_display_view,
            _np_widgets_content_active=_np_widgets_content_active,
            _resolve_receiver_lines_for_now_playing=_resolve_receiver_lines_for_now_playing,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _sync_status_bar_trt_substantive=_sync_status_bar_trt_substantive,
            _warm_status_bar_blits=_warm_status_bar_blits,
            apple_tv_playback_clock=apple_tv_playback_clock,
            current_apple_tv=current_apple_tv,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
        )

        _remove_saved_player_device = _bind_deps(
            _core_settings_ui._remove_saved_player_device,
            _clear_reported_position_stall_stamp=_clear_reported_position_stall_stamp,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            _sync_status_bar_visibility_for_playback=_sync_status_bar_visibility_for_playback,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_busy=apple_tv_busy,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            apple_tv_playback_clock=apple_tv_playback_clock,
            current_apple_tv=current_apple_tv,
            describe_current_apple_tv=describe_current_apple_tv,
            root=root,
            streaming_slot_holder=streaming_slot_holder,
        )

        _sync_streaming_badge_from_playback_sources = _bind_deps(
            _core_now_playing._sync_streaming_badge_from_playback_sources,
            _PROJECT_DIR=_PROJECT_DIR,
            _apply_netflix_backdrop_when_running=_apply_netflix_backdrop_when_running,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            playback_overlay_widget=playback_overlay_widget,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            streaming_badge_state=streaming_badge_state,
        )

        _return_to_landing_if_atv_idle = _bind_deps(
            _core_saver_state._return_to_landing_if_atv_idle,
            _PIGEON_EXT=_PIGEON_EXT,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _clear_playback_artwork_caches=_clear_playback_artwork_caches,
            _disp_fit=_disp_fit,
            _program_audio_session=_program_audio_session,
            _refresh_content_indicator=_refresh_content_indicator,
            _resolve_receiver_lines_for_now_playing=_resolve_receiver_lines_for_now_playing,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _sync_status_bar_visibility_for_playback=_sync_status_bar_visibility_for_playback,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _warm_status_bar_blits=_warm_status_bar_blits,
            _warm_tmdb_logo_patch=_warm_tmdb_logo_patch,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            landing_scene_design_bgr=landing_scene_design_bgr,
            last_frame=last_frame,
            playing=playing,
            receiver_standby_holder=receiver_standby_holder,
            render_once=_late(lambda: render_once, "render_once"),
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
            tmdb_logo_app_fallback_active=tmdb_logo_app_fallback_active,
            tmdb_logo_patch_bgra=tmdb_logo_patch_bgra,
            tmdb_logo_widget=tmdb_logo_widget,
            tmdb_logo_widget_view_six=tmdb_logo_widget_view_six,
            use_backdrop_scene=use_backdrop_scene,
        )

        _apply_hdmi_frame_check = _bind_deps(
            _core_device_control._apply_hdmi_frame_check,
            _bump_clock_saver_significant_device=_bump_clock_saver_significant_device,
            _note_metadata_activity=_note_metadata_activity,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _on_hdmi_frame_checked = _bind_deps(
            _core_device_control._on_hdmi_frame_checked,
            _apply_hdmi_frame_check=_apply_hdmi_frame_check,
            apple_tv_auto_state=apple_tv_auto_state,
            root=root,
        )

        _schedule_hdmi_frame_check_from_poll = _bind_deps(
            _core_device_control._schedule_hdmi_frame_check_from_poll,
            _on_hdmi_frame_checked=_on_hdmi_frame_checked,
            apple_tv_auto_state=apple_tv_auto_state,
        )

        _apple_tv_auto_poll_tick = _bind_deps(
            _core_device_control._apple_tv_auto_poll_tick,
            APPLE_TV_FAIL_POLL_MAX_MS=APPLE_TV_FAIL_POLL_MAX_MS,
            APPLE_TV_IDLE_POLL_MS=APPLE_TV_IDLE_POLL_MS,
            APPLE_TV_POLL_MS=APPLE_TV_POLL_MS,
            RECEIVER_POLL_MS=RECEIVER_POLL_MS,
            _PIGEON_EXT=_PIGEON_EXT,
            _apple_tv_auto_poll_tick=_late(lambda: _apple_tv_auto_poll_tick, "_apple_tv_auto_poll_tick"),
            _apple_tv_scan_timeout_s=_apple_tv_scan_timeout_s,
            _atv_metadata_is_content_idle=_atv_metadata_is_content_idle,
            _bump_clock_saver_significant_device=_bump_clock_saver_significant_device,
            _bump_clock_saver_significant_device_from_metadata=_bump_clock_saver_significant_device_from_metadata,
            _clear_displayed_tmdb_art_for_content_change=_clear_displayed_tmdb_art_for_content_change,
            _content_key_from_metadata=_content_key_from_metadata,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _pyatv_install_hint=_pyatv_install_hint,
            _refresh_content_indicator=_refresh_content_indicator,
            _refresh_observed_pairing_led_rows=_refresh_observed_pairing_led_rows,
            _return_to_landing_if_atv_idle=_return_to_landing_if_atv_idle,
            _schedule_hdmi_frame_check_from_poll=_schedule_hdmi_frame_check_from_poll,
            _seed_current_apple_tv_from_streaming_slot=_seed_current_apple_tv_from_streaming_slot,
            _store_music_artwork_from_metadata=_store_music_artwork_from_metadata,
            _sync_status_bar_visibility_for_playback=_sync_status_bar_visibility_for_playback,
            _sync_streaming_badge_from_playback_sources=_sync_streaming_badge_from_playback_sources,
            _tmdb_pref_from_metadata=_tmdb_pref_from_metadata,
            _tmdb_spawn_identity_changed=_tmdb_spawn_identity_changed,
            _update_atv_interaction_from_poll_metadata=_update_atv_interaction_from_poll_metadata,
            _update_status_bar_from_metadata=_update_status_bar_from_metadata,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_busy=apple_tv_busy,
            apple_tv_dashboard_track=apple_tv_dashboard_track,
            current_apple_tv=current_apple_tv,
            denon_vol_cache=denon_vol_cache,
            playback_overlay_widget=playback_overlay_widget,
            receiver_overlay_state=receiver_overlay_state,
            receiver_standby_holder=receiver_standby_holder,
            render_once=_late(lambda: render_once, "render_once"),
            resolve_metadata_tmdb_query=resolve_metadata_tmdb_query,
            root=root,
            skip_cache=skip_cache,
            spawn_tmdb_poster_fetch=spawn_tmdb_poster_fetch,
            streaming_slot_holder=streaming_slot_holder,
        )

        on_debug_streaming_slot_apple_tv = _bind_deps(
            _core_device_control.on_debug_streaming_slot_apple_tv,
            _PIGEON_EXT=_PIGEON_EXT,
            _open_find_device_dialog=_open_find_device_dialog,
            _pyatv_install_hint=_pyatv_install_hint,
            apple_tv_busy=apple_tv_busy,
            begin_apple_tv_operation=begin_apple_tv_operation,
            describe_current_apple_tv=describe_current_apple_tv,
            end_apple_tv_operation=end_apple_tv_operation,
            root=root,
            streaming_slot_holder=streaming_slot_holder,
        )

        _attach_hover_tooltip = _bind_deps(
            _core_settings_ui._attach_hover_tooltip,
            S_FONT_SMALL=S_FONT_SMALL,
            root=root,
        )

        _perform_tmdb_error_flag_retry = _bind_deps(
            _core_tmdb_flow._perform_tmdb_error_flag_retry,
            TMDB_ERROR_FLAG_RETRY_RULES=TMDB_ERROR_FLAG_RETRY_RULES,
            _PIGEON_EXT=_PIGEON_EXT,
            _alternate_tmdb_query_from_metadata=_alternate_tmdb_query_from_metadata,
            _clear_now_playing_view_caches=_clear_now_playing_view_caches,
            _mark_tmdb_missing_art=_mark_tmdb_missing_art,
            _raw_title_query_from_metadata=_raw_title_query_from_metadata,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _tmdb_retry_log_append=_tmdb_retry_log_append,
            _tmdb_spawn_identity=_tmdb_spawn_identity,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            apple_tv_auto_state=apple_tv_auto_state,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            spawn_tmdb_poster_fetch=spawn_tmdb_poster_fetch,
            tmdb_error_flag_retry_active=tmdb_error_flag_retry_active,
            tmdb_error_flag_retry_rule_idx=tmdb_error_flag_retry_rule_idx,
        )

        _perform_tmdb_artwork_retry = _bind_deps(
            _core_tmdb_flow._perform_tmdb_artwork_retry,
            _PIGEON_EXT=_PIGEON_EXT,
            _alternate_tmdb_query_from_metadata=_alternate_tmdb_query_from_metadata,
            _append_tmdb_retry_log_ui=_append_tmdb_retry_log_ui,
            _tmdb_retry_log_append=_tmdb_retry_log_append,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            apple_tv_playback_clock=apple_tv_playback_clock,
            spawn_tmdb_poster_fetch=spawn_tmdb_poster_fetch,
            tmdb_retry_rule_idx=tmdb_retry_rule_idx,
        )

        on_tmdb_retry_hotkey = _bind_deps(
            _core_input_keys.on_tmdb_retry_hotkey,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _last_tmdb_hotkey_mono=_last_tmdb_hotkey_mono,
            _perform_tmdb_artwork_retry=_perform_tmdb_artwork_retry,
            _widget_accepts_typing=_widget_accepts_typing,
        )

        on_tmdb_quality_error_report_hotkey = _bind_deps(
            _core_tmdb_flow.on_tmdb_quality_error_report_hotkey,
            TMDB_QUALITY_UNLOG_WINDOW_S=TMDB_QUALITY_UNLOG_WINDOW_S,
            _PIGEON_EXT=_PIGEON_EXT,
            _adjust_tmdb_quality_failure_delta=_adjust_tmdb_quality_failure_delta,
            _append_tmdb_quality_event_report_log=_append_tmdb_quality_event_report_log,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _clear_tmdb_quality_flag=_clear_tmdb_quality_flag,
            _last_tmdb_quality_report_mono=_last_tmdb_quality_report_mono,
            _perform_tmdb_error_flag_retry=_perform_tmdb_error_flag_retry,
            _schedule_tmdb_quality_auto_expire=_schedule_tmdb_quality_auto_expire,
            _trigger_tmdb_quality_toggle_overlay=_trigger_tmdb_quality_toggle_overlay,
            _widget_accepts_typing=_widget_accepts_typing,
            active_tmdb_display_title=active_tmdb_display_title,
            active_tmdb_title_key=active_tmdb_title_key,
            apple_tv_auto_state=apple_tv_auto_state,
            skip_cache=skip_cache,
            tmdb_error_flag_retry_active=tmdb_error_flag_retry_active,
            tmdb_error_flag_retry_rule_idx=tmdb_error_flag_retry_rule_idx,
            tmdb_quality_error_flag=tmdb_quality_error_flag,
            tmdb_quality_flag_set_mono=tmdb_quality_flag_set_mono,
        )

        purge_image_media_btn = tk.Button(
            content_buttons_row,
            text="Purge Image Media",
            command=on_purge_image_media,
            font=S_FONT_BTN,
            padx=8,
            pady=4,
        )
        purge_image_media_btn.pack(side=tk.LEFT, padx=(0, 8))
        if _PIGEON_EXT:
            _mq_glance = tk.Label(
                content_buttons_row,
                text="",
                fg="#e4e6ef",
                bg="#111",
                font=(_S, 13, "bold"),
                anchor=tk.W,
                justify=tk.LEFT,
            )
            _mq_glance.pack(side=tk.LEFT, padx=(8, 0), anchor=tk.CENTER)
            match_quality_glance_label_holder[0] = _mq_glance
            _attach_hover_tooltip(
                _mq_glance,
                "⌘⇧X / Ctrl+Shift+X toggles the quality flag: turning it on adds a persisted failure "
                "immediately; turning it off removes one (not below zero). Each new content event still "
                "scores one outcome after a successful populate (success, or FAILURE log only if the flag "
                "was on). Persisted in state.json; details + log on Advanced → TMDb.",
            )
            reset_mq_stats_btn = tk.Button(
                content_buttons_row,
                text="Reset match stats",
                command=on_reset_tmdb_match_quality_stats,
                font=S_FONT_BTN,
                padx=8,
                pady=4,
            )
            reset_mq_stats_btn.pack(side=tk.LEFT, padx=(10, 0))
            _attach_hover_tooltip(
                reset_mq_stats_btn,
                "Clears the ok / fail counts and % shown here (saved in state.json). Does not change "
                "tmdb_error_report.numbers, pigeonTMDBReport.csv, or tmdb_quality_event_reports.log.",
            )
            root.after_idle(_refresh_match_quality_glance_label)

        settings_footer_row = tk.Frame(settings_inner, bg="#111")
        settings_footer_row.pack(anchor=tk.W, fill=tk.X, pady=(16, 12))
        _frb = tk.Button(
            settings_footer_row,
            text="Reset",
            command=on_reset_pigeon_devices_and_media,
            font=S_FONT_BTN,
            padx=14,
            pady=6,
        )
        _frb.pack(side=tk.LEFT, padx=(0, 12))
        settings_footer_reset_holder[0] = _frb
        _fdb = tk.Button(
            settings_footer_row,
            text="Debug metadata",
            command=on_debug_streaming_slot_apple_tv,
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        _fdb.pack(side=tk.LEFT, padx=(0, 0))
        settings_footer_debug_holder[0] = _fdb
        tk.Label(
            settings_footer_row,
            text=f"Version {version_string()}",
            fg="#6d6d75",
            bg="#111",
            font=S_FONT_BODY,
        ).pack(side=tk.RIGHT, anchor=tk.E)
        _attach_hover_tooltip(
            _frb,
            "Clears all saved devices, pyatv credentials, discovery cache, and purges pigeonTMDB originals/backdrops/title-treatments.",
        )

        migrate_device_slots_from_legacy_if_needed()
        merge_legacy_saved_receivers_into_av_slot()
        streaming_slot_holder[0] = read_saved_streaming_device()
        avr_slot_holder[0] = read_saved_av_receiver()
        _seed_current_apple_tv_from_streaming_slot()
        # Prefer the location AV slot address over last_receiver — the latter can
        # linger on a stale IP after the Denon DHCP/address changes, which leaves
        # zone3 with an empty volume fraction (no red ring).
        _av_boot = avr_slot_holder[0]
        _av_adr = str((_av_boot or {}).get("address") or "").strip() if _av_boot else ""
        _last_rx_host = str(read_last_receiver().get("host") or "").strip()
        if _av_adr:
            if _av_adr != _last_rx_host:
                write_last_receiver(
                    host=_av_adr,
                    name=str(_av_boot.get("name") or "").strip() or None,
                    label=str(_av_boot.get("label") or "").strip() or None,
                    device_id=str(_av_boot.get("identifier") or "").strip() or None,
                )
            receiver_http_host["host"] = _av_adr
        else:
            receiver_http_host["host"] = _last_rx_host
        describe_current_apple_tv()
        _refresh_location_selector()
        _rebuild_paired_devices_panel()
        root.after(300, _schedule_refresh_pairing_leds)
        root.after(2500, _apple_tv_auto_poll_tick)
        root.after(PLAYBACK_UI_TICK_MS, _playback_ui_tick)

        submit_command_entry = _bind_deps(
            _core_tmdb_flow.submit_command_entry,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _last_command_submit_mono=_last_command_submit_mono,
            command_entry=command_entry,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
            hide_command_entry=hide_command_entry,
            parse_tmdb_command_phrase=parse_tmdb_command_phrase,
            spawn_tmdb_poster_fetch=spawn_tmdb_poster_fetch,
        )

        for _seq in ("<Return>", "<KeyPress-Return>"):
            command_entry.bind(_seq, submit_command_entry)
        command_entry.bind("<KP_Enter>", submit_command_entry)
        command_entry.bind("<KeyPress-KP_Enter>", submit_command_entry)

        label.bind("<Button-1>", on_click_focus, add="+")
        # Tap-to-wake: some platforms deliver release more reliably for “tap” than press alone.
        # While the saver layer is visible, a tap brightens it briefly (see CLOCK_SAVER_PEEK_S).
        _on_label_button_release_peek_or_bump = _bind_deps(
            _core_input_keys._on_label_button_release_peek_or_bump,
            CLOCK_SAVER_PEEK_S=CLOCK_SAVER_PEEK_S,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _clock_saver_for_compose=_clock_saver_for_compose,
            clock_saver_composite_bgra=clock_saver_composite_bgra,
            clock_saver_peek_until_mono=clock_saver_peek_until_mono,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
        )

        label.bind("<ButtonRelease-1>", _on_label_button_release_peek_or_bump, add="+")
        label.bind("<Double-Button-1>", on_double_click_scene, add="+")
        label.bind("<Button-3>", lambda e: try_cycle_dev_phase(None))

        # Omit command_entry: it needs local <Return> to submit before bind_all runs.
        for w in (
            root,
            shell,
            video_area,
            label,
            command_bar,
            settings_frame,
            settings_scroll_outer,
            settings_canvas,
            settings_inner,
            settings_footer_row,
        ):
            if w is not None:
                _prepend_hotkey_bindtag(w)

        _TAB_SEQS = (
            "<Tab>",
            "<KeyPress-Tab>",
            "<Key-Tab>",
            "<ISO_Left_Tab>",
            "<KeyPress-ISO_Left_Tab>",
            "<Shift-Tab>",
            "<Shift-KeyPress-Tab>",
            "<Shift-Key-Tab>",
            "<KP_Tab>",
            "<KeyPress-KP_Tab>",
        )
        for seq in _TAB_SEQS:
            root.bind_class(HOTKEY_BINDTAG, seq, on_tab_key)
        root.bind_class(HOTKEY_BINDTAG, "<Control-KeyPress-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<Control-Key-Tab>", on_ctrl_tab)
        # X11/Pi: class <Tab> is tk_focusNext and returns break before bind_all.
        for _tab_cls in (
            "Button",
            "Label",
            "Frame",
            "Canvas",
            "Toplevel",
            "Tk",
            "TFrame",
            "TLabel",
            "TButton",
            "TCheckbutton",
            "TRadiobutton",
            "Radiobutton",
            "Checkbutton",
            "Scale",
            "Listbox",
            "Entry",
            "TEntry",
            "Text",
        ):
            for seq in _TAB_SEQS:
                try:
                    root.bind_class(_tab_cls, seq, on_tab_key)
                except tk.TclError:
                    pass
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-s>", on_s_key)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-S>", on_s_key)

        on_return_overlay_command = _bind_deps(
            _core_tmdb_flow.on_return_overlay_command,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _widget_accepts_typing=_widget_accepts_typing,
            apple_tv_busy=apple_tv_busy,
            command_entry=command_entry,
            command_entry_visible=command_entry_visible,
            current_apple_tv=current_apple_tv,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
            show_command_entry=show_command_entry,
            streaming_slot_holder=streaming_slot_holder,
        )

        for seq in _TAB_SEQS:
            root.bind_all(seq, on_tab_key)
        # Do not bind_all(Return): on macOS that can run before the Entry binding and swallow the key.
        for _rseq in ("<Return>", "<KeyPress-Return>", "<KP_Enter>", "<KeyPress-KP_Enter>"):
            root.bind_class(HOTKEY_BINDTAG, _rseq, on_return_overlay_command)

        _send_player_play_pause_hotkey = _bind_deps(
            _core_device_control._send_player_play_pause_hotkey,
            _PIGEON_EXT=_PIGEON_EXT,
            apple_tv_busy=apple_tv_busy,
            current_apple_tv=current_apple_tv,
            streaming_slot_holder=streaming_slot_holder,
        )

        on_space_play = _bind_deps(
            _core_input_keys.on_space_play,
            DevPhase=DevPhase,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _handle_main_settings_action=_handle_main_settings_action,
            _last_space_mono=_last_space_mono,
            _send_player_play_pause_hotkey=_send_player_play_pause_hotkey,
            _widget_accepts_typing=_widget_accepts_typing,
            apply_saved_tmdb_backdrop_to_display=apply_saved_tmdb_backdrop_to_display,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
            render_once=_late(lambda: render_once, "render_once"),
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
            toggle_play=toggle_play,
            use_backdrop_scene=use_backdrop_scene,
        )

        # <KeyPress-Space> is invalid on some Tk builds (TclError: bad keysym "Space").
        for _space_seq in ("<space>", "<KeyPress-space>"):
            root.bind_all(_space_seq, on_space_play)

        on_display_view_digit = _bind_deps(
            _core_input_keys.on_display_view_digit,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            ViewOneLayout=ViewOneLayout,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _capture_last_view_one_layout_from_live_view=_capture_last_view_one_layout_from_live_view,
            _current_view_one_variant=_current_view_one_variant,
            _idle_saver_face_toggle_ok=_idle_saver_face_toggle_ok,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _toggle_clock_saver_force=_toggle_clock_saver_force,
            _vv_is_music=_vv_is_music,
            _widget_accepts_typing=_widget_accepts_typing,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
            main_settings_widget=main_settings_widget,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
            toggle_audio_meter_face=toggle_audio_meter_face,
            variant_has_alternate=variant_has_alternate,
            view_circles_widget=view_circles_widget,
            view_five_mode_holder=view_five_mode_holder,
            view_four_subview_holder=view_four_subview_holder,
            view_one_layout_holder=view_one_layout_holder,
        )

        for _dv_ch in ("0", "1", "2", "3", "4", "5", "6", "7", "8"):
            root.bind_all(f"<KeyPress-{_dv_ch}>", on_display_view_digit)

        on_arrow_remote = _bind_deps(
            _core_input_keys.on_arrow_remote,
            DevPhase=DevPhase,
            _PIGEON_EXT=_PIGEON_EXT,
            _nav_request=_nav_request,
            _widget_accepts_typing=_widget_accepts_typing,
            apple_tv_busy=apple_tv_busy,
            current_apple_tv=current_apple_tv,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            streaming_slot_holder=streaming_slot_holder,
        )

        for _ak in ("<KeyPress-Up>", "<KeyPress-Down>", "<KeyPress-Left>", "<KeyPress-Right>"):
            root.bind_all(_ak, on_arrow_remote)

        on_escape = _bind_deps(
            _core_input_keys.on_escape,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            command_entry_visible=command_entry_visible,
            hide_command_entry=hide_command_entry,
            quit_app=quit_app,
        )

        root.bind_all("<Escape>", on_escape)
        root.bind_all("<KeyPress-F9>", lambda e: try_cycle_dev_phase(None))
        root.bind_all("<KeyPress-F10>", on_f10_key)

        on_ctrl_shift_tab_advanced = _bind_deps(
            _core_input_keys.on_ctrl_shift_tab_advanced,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _last_adv_shift_tab_mono=_last_adv_shift_tab_mono,
            _open_advanced_capability_matrix=_open_advanced_capability_matrix,
            _widget_accepts_typing=_widget_accepts_typing,
        )

        for _adv_hot in (
            "<Control-Shift-KeyPress-Tab>",
            "<Control-Shift-Key-Tab>",
            "<Control-Shift-KeyPress-ISO_Left_Tab>",
        ):
            root.bind_all(_adv_hot, on_ctrl_shift_tab_advanced)
        for _stab in (
            "<KeyPress-ISO_Left_Tab>",
            "<Shift-KeyPress-Tab>",
            "<Shift-Key-Tab>",
        ):
            root.bind_all(_stab, on_shift_tab_dev_cycle)
        for _tmdb_key in ("<KeyPress-question>", "<Shift-KeyPress-slash>"):
            root.bind_all(_tmdb_key, on_tmdb_retry_hotkey)
        for _tmdb_qx in (
            "<Command-Shift-KeyPress-x>",
            "<Command-Shift-KeyPress-X>",
            "<Control-Shift-KeyPress-x>",
            "<Control-Shift-KeyPress-X>",
        ):
            root.bind_all(_tmdb_qx, on_tmdb_quality_error_report_hotkey)

        on_tmdb_match_mode_toggle = _bind_deps(
            _core_input_keys.on_tmdb_match_mode_toggle,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _last_tmdb_match_toggle_mono=_last_tmdb_match_toggle_mono,
            _widget_accepts_typing=_widget_accepts_typing,
        )

        on_dev_series_title_training_hotkey = _bind_deps(
            _core_input_keys.on_dev_series_title_training_hotkey,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            _PIGEON_EXT=_PIGEON_EXT,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _widget_accepts_typing=_widget_accepts_typing,
            apple_tv_auto_state=apple_tv_auto_state,
            dev_phase=dev_phase,
            display_view_holder=display_view_holder,
            root=root,
            spawn_tmdb_poster_fetch=spawn_tmdb_poster_fetch,
        )

        for _plus_key in (
            "<KeyPress-plus>",
            "<Shift-KeyPress-equal>",
            "<Shift-KeyPress-plus>",
            "<KeyPress-KP_Add>",
        ):
            root.bind_all(_plus_key, on_dev_series_title_training_hotkey)

        root.bind_all("<Control-Shift-KeyPress-m>", on_tmdb_match_mode_toggle)
        root.bind_all("<Control-Shift-KeyPress-M>", on_tmdb_match_mode_toggle)

        root.bind_all("<Control-Shift-KeyPress-s>", lambda e: toggle_scene(require_overlay=False))
        root.bind_all("<Control-Shift-KeyPress-S>", lambda e: toggle_scene(require_overlay=False))

        # Pixel-aspect override: hold P+A+R together to toggle auto ↔ off.
        _par_chord_held: set[str] = set()
        _par_chord_fired = [False]

        _on_par_chord_press = _bind_deps(
            _core_input_keys._on_par_chord_press,
            _par_chord_fired=_par_chord_fired,
            _par_chord_held=_par_chord_held,
            _widget_accepts_typing=_widget_accepts_typing,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
        )

        _on_par_chord_release = _bind_deps(
            _core_input_keys._on_par_chord_release,
            _par_chord_fired=_par_chord_fired,
            _par_chord_held=_par_chord_held,
        )

        for _par_ch in ("p", "a", "r", "P", "A", "R"):
            root.bind_all(f"<KeyPress-{_par_ch}>", _on_par_chord_press, add="+")
            root.bind_all(f"<KeyRelease-{_par_ch}>", _on_par_chord_release, add="+")

        _focus_when_mapped = _bind_deps(_core_input_keys._focus_when_mapped, root=root)

        root.bind("<Map>", _focus_when_mapped)
        root.after_idle(_focus_when_mapped)

        for _pigeon_act in ("<Button-1>", "<B1-Motion>", "<KeyPress>"):
            root.bind_all(_pigeon_act, _bump_pigeon_user_activity, add="+")

        # Serial rotary (non-HID USB): CW/CCW/PUSH → navigate / activate.
        # Prefer a direct callback so settings work even when Tk focus is elsewhere.
        _enter_main_settings_for_rotary = _bind_deps(
            _core_settings_ui._enter_main_settings_for_rotary,
            DevPhase=DevPhase,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
        )

        _on_rotary_action = _bind_deps(
            _core_input_keys._on_rotary_action,
            DevPhase=DevPhase,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _enter_main_settings_for_rotary=_enter_main_settings_for_rotary,
            _handle_main_settings_action=_handle_main_settings_action,
            _nav_request=_nav_request,
            dev_phase=dev_phase,
            main_settings_widget=main_settings_widget,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
        )

        _volume_rotary_fail_log_count = [0]
        _volume_rotary_ok_log_count = [0]
        _receiver_volume_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=16)

        _receiver_volume_worker = _bind_deps(
            _core_device_control._receiver_volume_worker,
            _clock_saver_volume=_clock_saver_volume,
            _note_volume_graphics=_note_volume_graphics,
            _receiver_volume_queue=_receiver_volume_queue,
            _volume_rotary_fail_log_count=_volume_rotary_fail_log_count,
            _volume_rotary_ok_log_count=_volume_rotary_ok_log_count,
            denon_vol_cache=denon_vol_cache,
            receiver_overlay_state=receiver_overlay_state,
            receiver_power_on_pending=receiver_power_on_pending,
            receiver_standby_holder=receiver_standby_holder,
            receiver_volume_cmd_busy=receiver_volume_cmd_busy,
            render_once=_late(lambda: render_once, "render_once"),
            root=root,
        )

        threading.Thread(
            target=_receiver_volume_worker,
            name="pigeon-receiver-volume",
            daemon=True,
        ).start()

        _queue_receiver_volume_action = _bind_deps(
            _core_device_control._queue_receiver_volume_action,
            _receiver_volume_queue=_receiver_volume_queue,
            avr_slot_holder=avr_slot_holder,
        )

        _nudge_clock_saver_volume = _bind_deps(
            _core_saver_state._nudge_clock_saver_volume,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _clock_saver_volume=_clock_saver_volume,
            _clock_saver_volume_raw=_clock_saver_volume_raw,
            _note_volume_graphics=_note_volume_graphics,
            _note_zone3_volume_takeover=_note_zone3_volume_takeover,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            clock_saver_force_on=clock_saver_force_on,
            denon_vol_cache=denon_vol_cache,
            receiver_overlay_state=receiver_overlay_state,
            render_once=_late(lambda: render_once, "render_once"),
            skip_cache=skip_cache,
        )

        _on_volume_rotary_action = _bind_deps(
            _core_input_keys._on_volume_rotary_action,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _note_zone3_volume_takeover=_note_zone3_volume_takeover,
            _nudge_clock_saver_volume=_nudge_clock_saver_volume,
            _queue_receiver_volume_action=_queue_receiver_volume_action,
            _volume_rotary_fail_log_count=_volume_rotary_fail_log_count,
            apple_tv_busy=apple_tv_busy,
            current_apple_tv=current_apple_tv,
            receiver_power_on_pending=receiver_power_on_pending,
            receiver_power_on_until=receiver_power_on_until,
            receiver_standby_holder=receiver_standby_holder,
            streaming_slot_holder=streaming_slot_holder,
        )

        _play_pause_gpio_last_mono = [0.0]

        _on_play_pause_gpio_action = _bind_deps(
            _core_input_keys._on_play_pause_gpio_action,
            _bump_pigeon_user_activity=_bump_pigeon_user_activity,
            _play_pause_gpio_last_mono=_play_pause_gpio_last_mono,
            _send_player_play_pause_hotkey=_send_player_play_pause_hotkey,
        )

        try:
            from pigeon.rotary_serial import start_rotary_serial_listener

            start_rotary_serial_listener(
                root,
                on_action=_on_rotary_action,
                on_volume_action=_on_volume_rotary_action,
                on_play_pause_action=_on_play_pause_gpio_action,
            )
        except Exception as _rotary_exc:
            sys.stderr.write(f"pigeon: rotary_serial: not started: {_rotary_exc}\n")
            sys.stderr.flush()

        _apply_shell_size = _bind_deps(
            _core_stage_render._apply_shell_size,
            SceneFit=SceneFit,
            _PIGEON_EXT=_PIGEON_EXT,
            _app_logo_clock_saver_style_now=_app_logo_clock_saver_style_now,
            backdrop_app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
            backdrop_master_bgr=backdrop_master_bgr,
            black_photo=black_photo,
            display_dims=display_dims,
            fit_holder=fit_holder,
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            skip_cache=skip_cache,
            sync_developer_chrome=sync_developer_chrome,
            use_backdrop_scene=use_backdrop_scene,
        )

        _on_shell_configure = _bind_deps(
            _core_input_keys._on_shell_configure,
            _apply_shell_size=_apply_shell_size,
            shell=shell,
        )

        sync_developer_chrome()

        render_once = _bind_deps(
            _core_stage_render.render_once,
            BACKDROP_BRIGHTNESS=BACKDROP_BRIGHTNESS,
            DevPhase=DevPhase,
            DisplayView=DisplayView,
            SKIP_POST_SPLASH_STARTUP_TRANSITION=SKIP_POST_SPLASH_STARTUP_TRANSITION,
            STARTUP_AUTO_RESTORE_SAVED_BACKDROP=STARTUP_AUTO_RESTORE_SAVED_BACKDROP,
            STARTUP_PIGEON_WORDMARK_MAX_S=STARTUP_PIGEON_WORDMARK_MAX_S,
            THEATER_IDLE_DIM_ENABLED=THEATER_IDLE_DIM_ENABLED,
            _PIGEON_EXT=_PIGEON_EXT,
            _apply_brightness=_apply_brightness,
            _atv_idle_monochrome_active=_atv_idle_monochrome_active,
            _audio_capture_wanted=_audio_capture_wanted,
            _backdrop_active_for_view=_backdrop_active_for_view,
            _bgr_to_tk_image=_bgr_to_tk_image,
            _black_screen_bgr=_black_screen_bgr,
            _blend_tmdb_quality_flag_badge=_blend_tmdb_quality_flag_badge,
            _blend_tmdb_quality_toggle_overlay=_blend_tmdb_quality_toggle_overlay,
            _blend_view_four_debug=_blend_view_four_debug,
            _capture_splash_underlay=_late(lambda: _capture_splash_underlay, "_capture_splash_underlay"),
            _clock_saver_for_compose=_clock_saver_for_compose,
            _clock_saver_volume_raw=_clock_saver_volume_raw,
            _clock_startup_intro_opacity=_clock_startup_intro_opacity,
            _compose_idle_strength_holder=_compose_idle_strength_holder,
            _compose_shown_frame=_compose_shown_frame,
            _effective_display_view=_effective_display_view,
            _idle_audio_listen=_idle_audio_listen,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _location_toast_alpha=_location_toast_alpha,
            _maybe_exit_settings_menus_on_idle=_maybe_exit_settings_menus_on_idle,
            _np_drawing_live_audio=_np_drawing_live_audio,
            _np_wants_live_audio=_np_wants_live_audio,
            _record_live_audio_timing=_record_live_audio_timing,
            _render_after_id=_render_after_id,
            _schedule_render_oneshot=_late(lambda: _schedule_render_oneshot, "_schedule_render_oneshot"),
            _set_playback_overlay_clock_saver_volume_flag=_set_playback_overlay_clock_saver_volume_flag,
            _settings_audio_led_listen=_settings_audio_led_listen,
            _settings_menu_is_static=_settings_menu_is_static,
            _show_paused_row_overlay=_show_paused_row_overlay,
            _startup_splash_complete=_startup_splash_complete,
            _tmdb_quality_toggle_overlay_state=_tmdb_quality_toggle_overlay_state,
            _update_idle_dim_strength=_update_idle_dim_strength,
            _update_label_photo_from_bgr=_update_label_photo_from_bgr,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _volume_lines=_volume_lines,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            _warm_status_bar_blits=_warm_status_bar_blits,
            apply_saved_tmdb_backdrop_to_display=apply_saved_tmdb_backdrop_to_display,
            backdrop_master_bgr=backdrop_master_bgr,
            black_photo=black_photo,
            brightness_current=brightness_current,
            brightness_duration_s=brightness_duration_s,
            brightness_from=brightness_from,
            brightness_t0=brightness_t0,
            brightness_target=brightness_target,
            clock_saver_peek_until_mono=clock_saver_peek_until_mono,
            dev_phase=dev_phase,
            display_dims=display_dims,
            display_view_holder=display_view_holder,
            frame_interval_ms=frame_interval_ms,
            label=label,
            label_live_photo=label_live_photo,
            last_frame=last_frame,
            latest_meter_cache_key=latest_meter_cache_key,
            latest_visualizer_cache_key=latest_visualizer_cache_key,
            lerp_bgr_red_monochrome=lerp_bgr_red_monochrome,
            main_settings_widget=main_settings_widget,
            paused_interval_ms=paused_interval_ms,
            playback_overlay_flags=playback_overlay_flags,
            playing=playing,
            post_splash_mono=post_splash_mono,
            receiver_overlay_state=receiver_overlay_state,
            root=root,
            saved_backdrop_master_bgr=saved_backdrop_master_bgr,
            scaled_display=scaled_display,
            scaled_version=scaled_version,
            scene_enabled=scene_enabled,
            skip_cache=skip_cache,
            status_bar_widget=status_bar_widget,
            sync_audio_meter_capture=sync_audio_meter_capture,
            tmdb_quality_error_flag=tmdb_quality_error_flag,
            use_backdrop_scene=use_backdrop_scene,
            view_circles_widget=view_circles_widget,
            view_five_mode_holder=view_five_mode_holder,
            view_four_subview_holder=view_four_subview_holder,
            view_one_layout_holder=view_one_layout_holder,
        )

        _remove_saved_receiver_device = _bind_deps(
            _core_settings_ui._remove_saved_receiver_device,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            apple_tv_busy=apple_tv_busy,
            avr_slot_holder=avr_slot_holder,
            describe_current_apple_tv=describe_current_apple_tv,
            playback_overlay_widget=playback_overlay_widget,
            receiver_http_host=receiver_http_host,
            render_once=render_once,
            root=root,
            skip_cache=skip_cache,
        )

        set_current_receiver_only = _bind_deps(
            _core_device_control.set_current_receiver_only,
            _rebuild_paired_devices_panel=_rebuild_paired_devices_panel,
            _schedule_refresh_pairing_leds=_schedule_refresh_pairing_leds,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            describe_current_apple_tv=describe_current_apple_tv,
            playback_overlay_widget=playback_overlay_widget,
            receiver_http_host=receiver_http_host,
            render_once=render_once,
            skip_cache=skip_cache,
        )

        _paint_coalesced_settings_nav = _bind_deps(
            _core_settings_ui._paint_coalesced_settings_nav,
            _nav_coalescer_holder=_nav_coalescer_holder,
            main_settings_widget=main_settings_widget,
            render_once=render_once,
            skip_cache=skip_cache,
        )

        try:
            from pigeon.nav_coalesce import NavPaintCoalescer

            _nav_coalescer_holder[0] = NavPaintCoalescer(
                after_idle=root.after_idle,
                after=root.after,
                cancel=root.after_cancel,
                paint=_paint_coalesced_settings_nav,
            )
        except Exception:
            _nav_coalescer_holder[0] = None

        _request_settings_nav_paint = _bind_deps(
            _core_settings_ui._request_settings_nav_paint,
            _nav_coalescer_holder=_nav_coalescer_holder,
            main_settings_widget=main_settings_widget,
            render_once=render_once,
            skip_cache=skip_cache,
        )

        _nav_request[0] = _request_settings_nav_paint

        _invoke_render_after = _bind_deps(
            _core_stage_render._invoke_render_after,
            _render_after_id=_render_after_id,
            render_once=render_once,
        )

        _schedule_render_oneshot = _bind_deps(
            _core_stage_render._schedule_render_oneshot,
            _invoke_render_after=_invoke_render_after,
            _render_after_id=_render_after_id,
            root=root,
        )

        _volume_quick_busy = [False]

        _note_volume_source_lines = _bind_deps(
            _core_device_control._note_volume_source_lines,
            denon_vol_cache=denon_vol_cache,
        )

        _commit_receiver_volume = _bind_deps(
            _core_device_control._commit_receiver_volume,
            _clock_saver_volume=_clock_saver_volume,
            _note_volume_graphics=_note_volume_graphics,
            _remember_clock_saver_volume=_remember_clock_saver_volume,
            denon_vol_cache=denon_vol_cache,
            receiver_overlay_state=receiver_overlay_state,
        )

        _on_denon_telnet_volume = _bind_deps(
            _core_device_control._on_denon_telnet_volume,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _commit_receiver_volume=_commit_receiver_volume,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _note_volume_source_lines=_note_volume_source_lines,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _volume_lines=_volume_lines,
            clock_saver_force_on=clock_saver_force_on,
            render_once=render_once,
            root=root,
            skip_cache=skip_cache,
        )

        _bind_receiver_volume_hub = _bind_deps(
            _core_device_control._bind_receiver_volume_hub,
            _on_denon_telnet_volume=_on_denon_telnet_volume,
        )

        _quick_receiver_volume_poll = _bind_deps(
            _core_device_control._quick_receiver_volume_poll,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _commit_receiver_volume=_commit_receiver_volume,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _note_volume_source_lines=_note_volume_source_lines,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _volume_lines=_volume_lines,
            _volume_quick_busy=_volume_quick_busy,
            clock_saver_force_on=clock_saver_force_on,
            denon_vol_cache=denon_vol_cache,
            receiver_http_host=receiver_http_host,
            render_once=render_once,
            root=root,
            skip_cache=skip_cache,
        )

        _receiver_volume_poll_tick = _bind_deps(
            _core_device_control._receiver_volume_poll_tick,
            RECEIVER_VOLUME_POLL_MS=RECEIVER_VOLUME_POLL_MS,
            _PIGEON_EXT=_PIGEON_EXT,
            _bind_receiver_volume_hub=_bind_receiver_volume_hub,
            _quick_receiver_volume_poll=_quick_receiver_volume_poll,
            _receiver_volume_poll_tick=_late(lambda: _receiver_volume_poll_tick, "_receiver_volume_poll_tick"),
            receiver_http_host=receiver_http_host,
            root=root,
        )

        _receiver_poll_tick = _bind_deps(
            _core_device_control._receiver_poll_tick,
            RECEIVER_POLL_MS=RECEIVER_POLL_MS,
            _PIGEON_EXT=_PIGEON_EXT,
            _bind_receiver_volume_hub=_bind_receiver_volume_hub,
            _bump_clock_saver_significant_device=_bump_clock_saver_significant_device,
            _clock_saver_for_compose=_clock_saver_for_compose,
            _clock_saver_receiver_off=_clock_saver_receiver_off,
            _clock_saver_volume=_clock_saver_volume,
            _denon_telnet_audio_fallback=_denon_telnet_audio_fallback,
            _idle_audio_meter_active=_idle_audio_meter_active,
            _note_volume_graphics=_note_volume_graphics,
            _note_volume_source_lines=_note_volume_source_lines,
            _paint_boolean_led=_paint_boolean_led,
            _quick_receiver_volume_poll=_quick_receiver_volume_poll,
            _receiver_poll_tick=_late(lambda: _receiver_poll_tick, "_receiver_poll_tick"),
            _refresh_observed_pairing_led_rows=_refresh_observed_pairing_led_rows,
            _remember_clock_saver_volume=_remember_clock_saver_volume,
            _sync_now_playing_screen_state=_sync_now_playing_screen_state,
            _sync_streaming_badge_from_playback_sources=_sync_streaming_badge_from_playback_sources,
            _view_one_uses_now_playing_screen=_view_one_uses_now_playing_screen,
            _warm_playback_overlay_blits=_warm_playback_overlay_blits,
            apple_tv_auto_state=apple_tv_auto_state,
            avr_slot_holder=avr_slot_holder,
            clock_saver_force_on=clock_saver_force_on,
            denon_vol_cache=denon_vol_cache,
            receiver_http_host=receiver_http_host,
            receiver_overlay_state=receiver_overlay_state,
            receiver_panel_led_holder=receiver_panel_led_holder,
            receiver_poll_busy=receiver_poll_busy,
            receiver_power_on_pending=receiver_power_on_pending,
            receiver_power_on_until=receiver_power_on_until,
            receiver_standby_holder=receiver_standby_holder,
            receiver_telnet_debug_holder=receiver_telnet_debug_holder,
            receiver_volume_cmd_busy=receiver_volume_cmd_busy,
            render_once=render_once,
            root=root,
            skip_cache=skip_cache,
            streaming_slot_holder=streaming_slot_holder,
        )

        _capture_splash_underlay = _bind_deps(
            _core_startup._capture_splash_underlay,
            WINDOW_H=WINDOW_H,
            WINDOW_W=WINDOW_W,
            _present_frame_to_display=_present_frame_to_display,
            _splash_underlay_bgr=_splash_underlay_bgr,
            startup_ph=startup_ph,
        )

        _splash_paint_view_one_under_overlay = _bind_deps(
            _core_startup._splash_paint_view_one_under_overlay,
            _PIGEON_EXT=_PIGEON_EXT,
            _warm_view_one_under_splash=_warm_view_one_under_splash,
            render_once=render_once,
            root=root,
            skip_cache=skip_cache,
        )

        root.after(0, _splash_paint_view_one_under_overlay)

        shell.bind("<Configure>", _on_shell_configure)

        _warm_view_one_under_splash(phase="full-warm-bootstrap-end")
        _enable_now_playing_screen()
        skip_cache[0] = None
        t_render0 = time.monotonic()
        render_once()
        _log_view_one_startup_phase(f"first-render ({(time.monotonic() - t_render0) * 1000.0:.0f} ms)")
        root.after(600, _receiver_poll_tick)
        root.after(700, _receiver_volume_poll_tick)

        if _PIGEON_EXT:
            tk.Widget.pack = _tk_pack_orig  # type: ignore[method-assign]
            tk.Widget.grid = _tk_grid_orig  # type: ignore[method-assign]
            tk.Widget.place = _tk_place_orig  # type: ignore[method-assign]
            try:
                root.update()
            except tk.TclError:
                pass
        bootstrap_done[0] = True
        _splash_clock_refresh_stop[0] = True
        _log_view_one_startup_phase("bootstrap-done")
        # Splash already finished before bootstrap started; run the deferred post-splash hook.
        if startup_ph[0] is None:
            _finish_post_splash_startup_transition()
        else:
            _try_remove_splash_overlay()

    if _PIGEON_EXT:
        # Play the splash at full rate on a free UI thread. Heavy bootstrap used to run in
        # parallel and starve ``after()``, freezing the last splash frame for many seconds.
        def _bootstrap_after_splash() -> None:
            if not splash_anim_done[0]:
                root.after(16, _bootstrap_after_splash)
                return
            try:
                sys.stderr.write(
                    f"pigeon: starting bootstrap after splash "
                    f"+{time.monotonic() - _app_startup_mono:.3f}s\n"
                )
                sys.stderr.flush()
            except Exception:
                pass
            bootstrap()

        root.after_idle(splash_tick)
        root.after_idle(_live_clock_until_compose)
        root.after_idle(_bootstrap_after_splash)
    else:
        root.after(1, bootstrap)
    try:
        root.mainloop()
    finally:
        _restore_desktop_chrome()
        if cap[0] is not None:
            cap[0].release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
