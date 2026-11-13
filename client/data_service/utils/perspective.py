"""Camera lens + homography + TPS residual correction for camera -> floorplan mapping.

Ported from cctv-reid `backend/app/services/perspective.py` so both projects
fit the exact same model. Intentionally kept behavior-identical (same
constants, same CV procedure, same fallback order) so results match across
projects; only the module path and this header differ. The branch services'
copy (dlc_local/floorplan_service/perspective.py) has the lens model too.

Method recap:
- H is fit with USAC_MAGSAC (thresh 3.0, dst units) then refit with
  least-squares (method=0) on the inliers. The persisted `matrix` is this H.
- A wide-angle CCTV lens bends the picture most near its edges, which H
  cannot follow: MAGSAC dropped correctly marked points there as outliers.
  So src (a camera picture of CAMERA_ASPECT) is first undistorted radially
  (k1, k2 about its centre) and one H maps the result onto dst, all fitted
  together by robust least squares, points more than MAGSAC_THRESH off left
  out. This lens + H replaces H when it predicts held-out points better
  (5-fold CV over every point) and turns no part of the floor the points
  mark over (_lens_holds): fitted to a few points it could.
  Floorplan -> camera fits pass camera_lens=False.
- What that base model (lens + H, or H) leaves: when the user supplies
  enough correspondences (>= 15 inliers) we fit a thin-plate-spline on the
  *residual* (dst - base(src)) of the inliers and add it back on top.
- smoothing is chosen from [0.1, 1, 10, 100, 1000] with 5-fold CV
  (fixed seed => deterministic). If the best TPS CV error is not better than
  doing nothing (mean |residual|), TPS is disabled.
- Outside the convex hull of src inliers (Delaunay.find_simplex < 0)
  we fall back to the base model only — never extrapolate TPS.
- Non-finite outputs return (x, y, False), same as the legacy behavior.
- RBF/Delaunay objects are never serialized: callers persist raw
  src/dst points (camera_calibration.src_points/dst_points) and refit on
  load. Models are cached in memory keyed by (namespace, points hash).

Units: same as dst. Our floorplan UI works in percent 0..100 on both sides,
so MAGSAC_THRESH=3.0 and the warning margins below are in those units.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

try:  # scipy is a hard dep in requirements.txt, but keep import soft for tests
    from scipy.interpolate import RBFInterpolator
    from scipy.optimize import least_squares
    from scipy.spatial import Delaunay

    _SCIPY_OK = True
except Exception:  # noqa: BLE001
    RBFInterpolator = None  # type: ignore[assignment]
    least_squares = None  # type: ignore[assignment]
    Delaunay = None  # type: ignore[assignment]
    _SCIPY_OK = False

SMOOTHING_GRID: Tuple[float, ...] = (0.1, 1.0, 10.0, 100.0, 1000.0)
MIN_INLIERS_FOR_TPS = 15
MAGSAC_THRESH = 3.0
CV_FOLDS = 5
CV_SEED = 0
# The lens model is round only in the picture's true proportions (width /
# height), which the percent points do not carry: the cameras are 16:9.
CAMERA_ASPECT = 16.0 / 9.0
MIN_POINTS_FOR_LENS = 8  # k1, k2 and H: 10 numbers
LENS_K_BOUND = 1.0  # |k1|, |k2|; the 13 Oct cameras fit 0.23-0.39 and about 0
LENS_CHECK_GRID = 25  # points per side of the picture checked for folds (_lens_holds)
LENS_CHECK_GROW = 1.2  # the marked floor (the camera points' hull) grown by this about its centre
# apply_mapping_inverse: how far (dst units) the solved point may still map
# from the target. Next to the TPS hull's edge the model steps, and the solve
# stops 0.1-0.4 short (13 Oct ch21 / ch24), well inside a calibration's own
# 1-2 per-point error.
INVERSE_TOLERANCE = 0.5


# --------------------------------------------------------------------------- helpers

def _to_pairs(pts: Sequence[Any]) -> Optional[np.ndarray]:
    """Accept flat [x0,y0,x1,y1,...] or [[x,y],...] / [{x,y},...] -> (N,2)."""
    try:
        if pts is None:
            return None
        arr = np.asarray(pts, dtype=np.float64)
        if arr.size == 0:
            return None
        if arr.ndim == 1:
            if arr.size % 2 != 0:
                return None
            arr = arr.reshape(-1, 2)
        elif arr.ndim == 2 and arr.shape[1] == 2:
            pass
        else:
            return None
        if not np.all(np.isfinite(arr)):
            return None
        return arr
    except Exception:
        return None


def _normalize_dict_points(pts: Sequence[Any]) -> Optional[np.ndarray]:
    """Handle [{x,y},...] in addition to plain arrays."""
    try:
        if pts and isinstance(pts[0], dict):  # type: ignore[index]
            flat = [[float(p.get("x", 0)), float(p.get("y", 0))] for p in pts]  # type: ignore[union-attr]
            return np.asarray(flat, dtype=np.float64)
    except Exception:
        return None
    return None


def _as_xy(pts: Sequence[Any]) -> Optional[np.ndarray]:
    dict_form = _normalize_dict_points(pts)
    if dict_form is not None:
        return dict_form if np.all(np.isfinite(dict_form)) else None
    return _to_pairs(pts)


def _apply_h(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply 3x3 homography to (N,2) -> (N,2). Non-finite-safe (may be inf)."""
    ones = np.ones((pts.shape[0], 1), dtype=np.float64)
    homo = np.concatenate([pts, ones], axis=1) @ H.T
    w = homo[:, 2:3]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = homo[:, :2] / w
    return out


def _fit_homography_magsac(
    src: np.ndarray, dst: np.ndarray
) -> Tuple[Optional[np.ndarray], np.ndarray]:
    """Fit H with USAC_MAGSAC then refit LS on inliers. Returns (H, inlier_mask)."""
    n = src.shape[0]
    if n < 4:
        return None, np.zeros(n, dtype=bool)
    H_init: Optional[np.ndarray] = None
    mask: Optional[np.ndarray] = None
    try:
        H_init, mask = cv2.findHomography(
            src.astype(np.float64), dst.astype(np.float64),
            cv2.USAC_MAGSAC, MAGSAC_THRESH,
        )
    except Exception:
        H_init, mask = None, None
    if H_init is None:
        # Fallback: legacy RANSAC so old calibrations never hard-fail.
        try:
            H_init, mask = cv2.findHomography(
                src.astype(np.float64), dst.astype(np.float64),
                cv2.RANSAC, MAGSAC_THRESH,
            )
        except Exception:
            return None, np.zeros(n, dtype=bool)
    if H_init is None:
        return None, np.zeros(n, dtype=bool)
    H_init = np.asarray(H_init, dtype=np.float64)
    if mask is None:
        inliers = np.ones(n, dtype=bool)
    else:
        inliers = np.asarray(mask, dtype=np.uint8).ravel().astype(bool)
        if inliers.size != n:
            inliers = np.ones(n, dtype=bool)
    # Refit least-squares on inliers.
    if int(inliers.sum()) >= 4:
        try:
            H_ls, _ = cv2.findHomography(
                src[inliers].astype(np.float64),
                dst[inliers].astype(np.float64),
                0,  # least squares
            )
            if H_ls is not None:
                H_init = np.asarray(H_ls, dtype=np.float64)
        except Exception:
            pass
    return H_init, inliers


def _cv_folds(n: int, folds: int = CV_FOLDS, seed: int = CV_SEED) -> List[np.ndarray]:
    rng = np.random.RandomState(seed)
    idx = np.arange(n)
    rng.shuffle(idx)
    return list(np.array_split(idx, folds))


def _cv_errors_h_only(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """5-fold CV L2 error of plain-H mapping (per inlier point)."""
    n = src.shape[0]
    errs = np.full(n, np.nan, dtype=np.float64)
    if n < 4:
        return errs
    for fold in _cv_folds(n):
        test = np.asarray(fold, dtype=int)
        train_mask = np.ones(n, dtype=bool)
        train_mask[test] = False
        if int(train_mask.sum()) < 4:
            continue
        try:
            H, _ = cv2.findHomography(src[train_mask], dst[train_mask], 0)
        except Exception:
            continue
        if H is None:
            continue
        pred = _apply_h(np.asarray(H, dtype=np.float64), src[test])
        with np.errstate(invalid="ignore"):
            errs[test] = np.linalg.norm(pred - dst[test], axis=1)
    # Any fold that failed (tiny n): fall back to train error for those points.
    missing = ~np.isfinite(errs)
    if np.any(missing):
        try:
            H_full, _ = cv2.findHomography(src, dst, 0)
            if H_full is not None:
                pred = _apply_h(np.asarray(H_full, dtype=np.float64), src[missing])
                errs[missing] = np.linalg.norm(pred - dst[missing], axis=1)
        except Exception:
            pass
    return errs


def _cv_errors(src: np.ndarray, dst: np.ndarray, fit, predict) -> np.ndarray:
    """5-fold CV L2 error per point of any model: ``fit(src, dst)`` -> model or None,
    ``predict(model, pts)``. NaN where a fold could not be fit."""
    n = src.shape[0]
    errs = np.full(n, np.nan, dtype=np.float64)
    for fold in _cv_folds(n):
        test = np.asarray(fold, dtype=int)
        train_mask = np.ones(n, dtype=bool)
        train_mask[test] = False
        if test.size == 0:
            continue
        model = fit(src[train_mask], dst[train_mask])
        if model is None:
            continue
        with np.errstate(invalid="ignore"):
            errs[test] = np.linalg.norm(predict(model, src[test]) - dst[test], axis=1)
    return errs


def _cv_score(errs: np.ndarray) -> float:
    """Mean CV error, each point counted at most 3 x MAGSAC_THRESH (a misplaced
    point or a failed fold, NaN, must not decide alone)."""
    cap = 3.0 * MAGSAC_THRESH
    return float(np.mean(np.minimum(np.where(np.isfinite(errs), errs, cap), cap)))


# --------------------------------------------------------------------------- lens

@dataclass
class LensModel:
    """The camera's lens, then one homography: src percent -> centred (r = 1 at
    the middle of the picture's longer side) -> p * (1 + k1 r^2 + k2 r^4) -> H -> dst."""

    k1: float
    k2: float
    H: np.ndarray  # (3,3) undistorted picture -> dst


def _undistort(pts: np.ndarray, k1: float, k2: float) -> np.ndarray:
    p = np.stack([(pts[:, 0] / 100.0 - 0.5) * 2.0,
                  (pts[:, 1] / 100.0 - 0.5) * 2.0 / CAMERA_ASPECT], axis=1)
    r2 = np.sum(p * p, axis=1)
    return p * (1.0 + k1 * r2 + k2 * r2 * r2)[:, None]


def _apply_lens(lens: LensModel, pts: np.ndarray) -> np.ndarray:
    return _apply_h(lens.H, _undistort(pts, lens.k1, lens.k2))


def _fit_lens_once(src: np.ndarray, dst: np.ndarray,
                   start: Optional[LensModel] = None) -> Optional[LensModel]:
    """k1, k2 and H by robust least squares (soft L1 at 1 dst unit) on every given point."""
    if least_squares is None or src.shape[0] < MIN_POINTS_FOR_LENS:
        return None
    if start is None:
        try:
            H0, _ = cv2.findHomography(_undistort(src, 0.0, 0.0), dst, 0)
        except Exception:
            return None
        if H0 is None or not np.all(np.isfinite(H0)) or abs(H0[2, 2]) < 1e-12:
            return None
        x0 = np.concatenate([[0.0, 0.0], (H0 / H0[2, 2]).ravel()[:8]])
    else:
        x0 = np.concatenate([[start.k1, start.k2], (start.H / start.H[2, 2]).ravel()[:8]])

    def residual(p: np.ndarray) -> np.ndarray:
        H = np.append(p[2:], 1.0).reshape(3, 3)
        return (_apply_h(H, _undistort(src, p[0], p[1])) - dst).ravel()

    lo = np.concatenate([[-LENS_K_BOUND, -LENS_K_BOUND], np.full(8, -np.inf)])
    hi = np.concatenate([[LENS_K_BOUND, LENS_K_BOUND], np.full(8, np.inf)])
    x0[:2] = np.clip(x0[:2], -LENS_K_BOUND + 1e-9, LENS_K_BOUND - 1e-9)
    try:
        if not np.all(np.isfinite(residual(x0))):
            return None
        fit = least_squares(residual, x0, bounds=(lo, hi), loss="soft_l1", f_scale=1.0, x_scale="jac")
    except Exception:
        return None
    if not np.all(np.isfinite(fit.x)):
        return None
    return LensModel(k1=float(fit.x[0]), k2=float(fit.x[1]), H=np.append(fit.x[2:], 1.0).reshape(3, 3))


def _fit_lens(src: np.ndarray, dst: np.ndarray) -> Tuple[Optional[LensModel], np.ndarray]:
    """Lens + H on every point, then again without those more than MAGSAC_THRESH off.

    Returns (model, inlier_mask); (None, all False) when it cannot be fit.
    """
    nothing = (None, np.zeros(src.shape[0], dtype=bool))
    lens = _fit_lens_once(src, dst)
    if lens is None:
        return nothing
    with np.errstate(invalid="ignore"):
        inliers = np.linalg.norm(_apply_lens(lens, src) - dst, axis=1) <= MAGSAC_THRESH
    if int(inliers.sum()) < MIN_POINTS_FOR_LENS:
        return nothing
    if not np.all(inliers):
        lens = _fit_lens_once(src[inliers], dst[inliers], start=lens)
        if lens is None:
            return nothing
    return lens, inliers


def _marked_floor(src: np.ndarray) -> Any:
    """The floor the camera points mark: their hull grown LENS_CHECK_GROW about its
    centre (people walk a little past the last marks); None when it cannot be built."""
    if Delaunay is None or src.shape[0] < 3:
        return None
    centre = src.mean(axis=0)
    try:
        return Delaunay(centre + (src - centre) * LENS_CHECK_GROW)
    except Exception:
        return None


def _folds_on(apply, area: Any) -> int:
    """Cells of a grid over the picture, inside ``area``, that ``apply`` (Nx2 percent -> Nx2) turns over."""
    xs = np.linspace(1.0, 99.0, LENS_CHECK_GRID)
    pts = np.array([[x, y] for y in xs for x in xs])
    grid = pts.reshape(LENS_CHECK_GRID, LENS_CHECK_GRID, 2)
    centres = ((grid[:-1, :-1] + grid[1:, 1:]) / 2.0).reshape(-1, 2)
    inside = (area.find_simplex(centres) >= 0).reshape(LENS_CHECK_GRID - 1, LENS_CHECK_GRID - 1)
    with np.errstate(all="ignore"):
        p = apply(pts).reshape(LENS_CHECK_GRID, LENS_CHECK_GRID, 2)
        a = p[:-1, 1:] - p[:-1, :-1]
        b = p[1:, :-1] - p[:-1, :-1]
        det = a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]
    seen = inside & np.isfinite(det)
    if not seen.any():
        return int(inside.sum())
    return int(np.sum(inside & (~np.isfinite(det) | (np.sign(det) != np.sign(np.median(det[seen]))))))


def _lens_holds(lens: LensModel, H: np.ndarray, src: np.ndarray) -> bool:
    """Lens + H turns no part of the marked floor (_marked_floor) over that H alone does not.

    Only that floor: above its horizon the top rows of a wide view can fold
    under lens + H, and no one walks there (2026-10-26: checked over the whole
    picture, UNS037 ch5 lost its lens and 58 good points went up to 20 off).
    """
    area = _marked_floor(src)
    if area is None:
        return True
    return _folds_on(lambda p: _apply_lens(lens, p), area) <= _folds_on(lambda p: _apply_h(H, p), area)


def _choose_lens(src: np.ndarray, dst: np.ndarray,
                 H: np.ndarray) -> Optional[Tuple[LensModel, np.ndarray]]:
    """Lens + H and its inliers, when it holds on the marked floor (_lens_holds) and
    predicts held-out points better than H alone."""
    lens, inliers = _fit_lens(src, dst)
    if lens is None or not _lens_holds(lens, H, src):
        return None
    lens_cv = _cv_errors(src, dst, lambda s, d: _fit_lens(s, d)[0], _apply_lens)
    h_cv = _cv_errors(src, dst, lambda s, d: _fit_homography_magsac(s, d)[0], _apply_h)
    if _cv_score(lens_cv) >= _cv_score(h_cv):
        return None
    return lens, inliers


# --------------------------------------------------------------------------- model

@dataclass
class CameraMapping:
    """Fitted camera->floorplan model. Never pickled to DB (refit on load)."""

    H: np.ndarray  # (3,3) float64, MAGSAC: the persisted matrix
    inlier_mask: np.ndarray  # (N,) bool over the *input* points
    src_points: np.ndarray  # (N,2) raw input src
    dst_points: np.ndarray  # (N,2) raw input dst
    use_tps: bool = False
    smoothing: Optional[float] = None
    rbf: Any = None  # RBFInterpolator | None
    hull: Any = None  # Delaunay | None
    metrics: Dict[str, Any] = field(default_factory=dict)
    lens: Optional[LensModel] = None  # base model when set (else H)


_MAPPING_CACHE: Dict[Tuple[str, ...], CameraMapping] = {}


def _cache_key_for_points(
    namespace: str, src: np.ndarray, dst: np.ndarray, extra: str = ""
) -> Tuple[str, ...]:
    import hashlib

    h = hashlib.sha256()
    h.update(np.ascontiguousarray(src, dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(dst, dtype=np.float64).tobytes())
    h.update(extra.encode())
    return (namespace, h.hexdigest())


def clear_mapping_cache(namespace: Optional[str] = None) -> None:
    """Invalidate cached models (call when calibration changes)."""
    if namespace is None:
        _MAPPING_CACHE.clear()
        return
    for k in [k for k in _MAPPING_CACHE if k and k[0] == namespace]:
        _MAPPING_CACHE.pop(k, None)


def _warn_points(src: np.ndarray, dst: np.ndarray) -> List[str]:
    warnings: List[str] = []
    try:
        n = src.shape[0]
        # Edge coverage: are there points near each image border?
        # NOTE (ai-camera divergence): "top" is intentionally NOT required.
        # A CCTV camera looks down from above — the top of its image is
        # ceiling/far field with nothing a shop can mark precisely, so
        # demanding top-edge points only nags about the impossible.
        margin = 15.0
        has_left = bool(np.any(src[:, 0] < margin))
        has_right = bool(np.any(src[:, 0] > 100 - margin))
        has_bottom = bool(np.any(src[:, 1] > 100 - margin))
        missing = [
            name for name, ok in
            (("left", has_left), ("right", has_right), ("bottom", has_bottom))
            if not ok
        ]
        if missing:
            warnings.append("ขอบภาพขาดจุด: " + ", ".join(missing))
        # Clustering: mean nearest-neighbor distance very small => points clumped.
        if n >= 3:
            from scipy.spatial.distance import cdist

            D = cdist(src, src)
            np.fill_diagonal(D, np.inf)
            nn = D.min(axis=1)
            if float(np.median(nn)) < 5.0:
                warnings.append("จุดกระจุกตัว (median NN < 5%) — กระจายจุดให้ทั่วภาพจะแม่นกว่า")
    except Exception:
        pass
    return warnings


def _summarize_errors(errs: np.ndarray) -> Dict[str, float]:
    finite = errs[np.isfinite(errs)]
    if finite.size == 0:
        return {"mean": float("nan"), "median": float("nan"), "p95": float("nan")}
    return {
        "mean": float(np.mean(finite)),
        "median": float(float(np.median(finite))),
        "p95": float(float(np.percentile(finite, 95))),
    }


def fit_mapping(src_pts: Sequence[Any], dst_pts: Sequence[Any],
                camera_lens: bool = True) -> Optional[CameraMapping]:
    """Fit lens + H (or H) + optional TPS residual correction.

    Units: same as dst (percent 0..100 on both sides in this project).
    ``camera_lens``: src is a camera picture whose lens is modelled; False
    for a floorplan -> camera fit. Deterministic (fixed CV seed). Returns
    None when < 4 correspondences.
    """
    src = _as_xy(src_pts)
    dst = _as_xy(dst_pts)
    if src is None or dst is None or src.shape[0] != dst.shape[0]:
        return None
    n = src.shape[0]
    if n < 4:
        return None

    H, inliers = _fit_homography_magsac(src, dst)
    if H is None:
        return None
    lens: Optional[LensModel] = None
    chosen = _choose_lens(src, dst, H) if camera_lens else None
    if chosen is not None:
        lens, inliers = chosen

    def base(pts: np.ndarray) -> np.ndarray:
        return _apply_lens(lens, pts) if lens is not None else _apply_h(H, pts)

    n_in = int(inliers.sum())
    src_in, dst_in = src[inliers], dst[inliers]

    base_in = base(src_in) if n_in else np.zeros((0, 2))
    with np.errstate(invalid="ignore"):
        resid_in = dst_in - base_in if n_in else np.zeros((0, 2))
        resid_norm = np.linalg.norm(resid_in, axis=1) if n_in else np.array([])
    baseline = float(np.mean(resid_norm)) if resid_norm.size else float("inf")

    # Base-model CV errors (per inlier point) — also the fallback metrics.
    if lens is not None:
        base_cv_errs = _cv_errors(src_in, dst_in, _fit_lens_once, _apply_lens)
        base_cv_errs = np.where(np.isfinite(base_cv_errs), base_cv_errs, resid_norm)
    else:
        base_cv_errs = _cv_errors_h_only(src_in, dst_in) if n_in >= 4 else resid_norm.copy()

    use_tps = False
    best_smoothing: Optional[float] = None
    best_cv = float("inf")
    best_per_point: Optional[np.ndarray] = None
    best_rbf: Any = None
    best_hull: Any = None

    if n_in >= MIN_INLIERS_FOR_TPS and _SCIPY_OK and np.all(np.isfinite(resid_in)):
        # 5-fold CV over smoothing grid. Error is measured in full dst space:
        # corrected = base(src_test) + RBF_train(src_test).
        for s in SMOOTHING_GRID:
            per_point = np.full(n_in, np.nan, dtype=np.float64)
            ok = True
            try:
                for fold in _cv_folds(n_in):
                    test = np.asarray(fold, dtype=int)
                    train_mask = np.ones(n_in, dtype=bool)
                    train_mask[test] = False
                    if int(train_mask.sum()) < 3:
                        ok = False
                        break
                    try:
                        rbf = RBFInterpolator(
                            src_in[train_mask], resid_in[train_mask],
                            kernel="thin_plate_spline", smoothing=float(s),
                        )
                        pred_resid = np.asarray(rbf(src_in[test]), dtype=np.float64)
                    except Exception:
                        ok = False
                        break
                    corrected = base_in[test] + pred_resid
                    with np.errstate(invalid="ignore"):
                        per_point[test] = np.linalg.norm(corrected - dst_in[test], axis=1)
                if not ok or not np.all(np.isfinite(per_point)):
                    continue
                cv_err = float(np.mean(per_point))
                if cv_err < best_cv:
                    best_cv = cv_err
                    best_smoothing = float(s)
                    best_per_point = per_point.copy()
            except Exception:
                continue
        # Accept TPS only if it beats doing nothing.
        if (
            best_smoothing is not None
            and best_per_point is not None
            and np.isfinite(best_cv)
            and best_cv < baseline
        ):
            try:
                best_rbf = RBFInterpolator(
                    src_in, resid_in,
                    kernel="thin_plate_spline", smoothing=float(best_smoothing),
                )
                # Sanity: final model must produce finite residuals on inliers.
                chk = np.asarray(best_rbf(src_in), dtype=np.float64)
                if np.all(np.isfinite(chk)):
                    best_hull = Delaunay(src_in) if _SCIPY_OK else None
                    use_tps = True
                else:
                    use_tps = False
            except Exception:
                use_tps = False
        if not use_tps:
            best_smoothing = None
            best_per_point = None

    if use_tps and best_per_point is not None:
        per_inlier_err = best_per_point
        smoothing = best_smoothing
    else:
        per_inlier_err = base_cv_errs
        smoothing = None
        use_tps = False
        best_rbf = None
        best_hull = None

    # Align per-point errors with the *input* order (outliers: base train error).
    per_point_all = np.full(n, np.nan, dtype=np.float64)
    in_idx = np.flatnonzero(inliers)
    per_point_all[in_idx] = per_inlier_err if per_inlier_err.size == n_in else np.nan
    out_idx = np.flatnonzero(~inliers)
    if out_idx.size:
        try:
            base_out = base(src[out_idx])
            with np.errstate(invalid="ignore"):
                per_point_all[out_idx] = np.linalg.norm(base_out - dst[out_idx], axis=1)
        except Exception:
            pass

    summary = _summarize_errors(per_inlier_err if per_inlier_err.size else per_point_all)
    metrics: Dict[str, Any] = {
        "n_points": int(n),
        "n_inliers": int(n_in),
        "n_outliers": int(n - n_in),
        "use_tps": bool(use_tps),
        "smoothing": smoothing,
        "mean_error": summary["mean"],
        "median_error": summary["median"],
        "p95_error": summary["p95"],
        "baseline_mean_residual": float(baseline) if np.isfinite(baseline) else None,
        "best_tps_cv_error": float(best_cv) if use_tps else None,
        "per_point_error": [float(v) if np.isfinite(v) else None for v in per_point_all.tolist()],
        "lens": {"k1": round(lens.k1, 4), "k2": round(lens.k2, 4)} if lens is not None else None,
        "warnings": _warn_points(src, dst),
    }
    return CameraMapping(
        H=np.asarray(H, dtype=np.float64),
        inlier_mask=np.asarray(inliers, dtype=bool),
        src_points=np.asarray(src, dtype=np.float64),
        dst_points=np.asarray(dst, dtype=np.float64),
        use_tps=bool(use_tps),
        smoothing=smoothing,
        rbf=best_rbf if use_tps else None,
        hull=best_hull if use_tps else None,
        metrics=metrics,
        lens=lens,
    )


def get_or_fit_mapping(
    src_pts: Sequence[Any],
    dst_pts: Sequence[Any],
    namespace: str = "default",
    extra: str = "",
    camera_lens: bool = True,
) -> Optional[CameraMapping]:
    """Cached fit keyed by points hash (callers invalidate via version/extra)."""
    src = _as_xy(src_pts)
    dst = _as_xy(dst_pts)
    if src is None or dst is None:
        return None
    key = _cache_key_for_points(namespace, src, dst, extra + ("" if camera_lens else "|no-lens"))
    hit = _MAPPING_CACHE.get(key)
    if hit is not None:
        return hit
    m = fit_mapping(src, dst, camera_lens=camera_lens)
    if m is not None:
        # Bound cache growth: calibration counts are tiny, 64 entries is plenty.
        if len(_MAPPING_CACHE) > 64:
            _MAPPING_CACHE.pop(next(iter(_MAPPING_CACHE)))
        _MAPPING_CACHE[key] = m
    return m


# --------------------------------------------------------------------------- apply

def _h_apply_single(H: np.ndarray, x: float, y: float) -> Tuple[float, float, bool]:
    try:
        # Manual math for float64 stability instead of cv2.perspectiveTransform.
        out = _apply_h(H, np.array([[float(x), float(y)]]))[0]
        rx, ry = float(out[0]), float(out[1])
        if not (np.isfinite(rx) and np.isfinite(ry)):
            return float(x), float(y), False
        return rx, ry, True
    except Exception:
        return float(x), float(y), False


def _base_apply_single(mapping: CameraMapping, x: float, y: float) -> Tuple[float, float, bool]:
    """The model under the TPS correction: lens + H when fitted, else H."""
    if mapping.lens is None:
        return _h_apply_single(mapping.H, x, y)
    try:
        out = _apply_lens(mapping.lens, np.array([[float(x), float(y)]]))[0]
        rx, ry = float(out[0]), float(out[1])
        if not (np.isfinite(rx) and np.isfinite(ry)):
            return float(x), float(y), False
        return rx, ry, True
    except Exception:
        return float(x), float(y), False


def apply_mapping(mapping: Optional[CameraMapping], x: float, y: float) -> Tuple[float, float, bool]:
    """Map one point. Falls back to the base model outside hull / when TPS disabled."""
    if mapping is None:
        return float(x), float(y), False
    hx, hy, ok = _base_apply_single(mapping, x, y)
    if not ok:
        return float(x), float(y), False
    if not mapping.use_tps or mapping.rbf is None or mapping.hull is None:
        return hx, hy, True
    try:
        inside = np.asarray(
            mapping.hull.find_simplex(np.array([[float(x), float(y)]])), dtype=int
        )[0] >= 0
    except Exception:
        inside = False
    if not inside:
        return hx, hy, True
    try:
        corr = np.asarray(
            mapping.rbf(np.array([[float(x), float(y)]])), dtype=np.float64
        ).ravel()
        if corr.size != 2 or not np.all(np.isfinite(corr)):
            return hx, hy, True
        rx, ry = float(hx + corr[0]), float(hy + corr[1])
        if not (np.isfinite(rx) and np.isfinite(ry)):
            return float(x), float(y), False
        return rx, ry, True
    except Exception:
        return hx, hy, True


def apply_mapping_inverse(mapping: Optional[CameraMapping], x: float, y: float) -> Tuple[float, float, bool]:
    """dst -> src by solving the fitted src -> dst model for the point (floorplan
    -> camera for a camera fit), so both directions agree point for point; a
    fit of the swapped points would put the camera's lens on the floorplan."""
    if mapping is None or least_squares is None:
        return float(x), float(y), False
    target = np.array([float(x), float(y)])
    try:
        start = _apply_h(np.linalg.inv(mapping.H), target[None, :])[0]
    except Exception:
        start = np.array([50.0, 50.0])
    if not np.all(np.isfinite(start)):
        start = np.array([50.0, 50.0])

    def residual(p: np.ndarray) -> np.ndarray:
        mx, my, ok = apply_mapping(mapping, p[0], p[1])
        return np.array([mx, my]) - target if ok else np.array([1e3, 1e3])

    try:
        fit = least_squares(residual, start, x_scale=10.0)
    except Exception:
        return float(x), float(y), False
    if not (np.all(np.isfinite(fit.x)) and np.max(np.abs(fit.fun)) < INVERSE_TOLERANCE):
        return float(x), float(y), False
    return float(fit.x[0]), float(fit.x[1]), True


def apply_mapping_batch(
    mapping: Optional[CameraMapping], pts: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Vectorized mapping for Nx2 input -> (Nx2 outputs, N bool ok)."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    n = pts.shape[0]
    out = pts.copy()
    ok = np.zeros(n, dtype=bool)
    if mapping is None or n == 0:
        return out, ok
    try:
        h_out = _apply_lens(mapping.lens, pts) if mapping.lens is not None else _apply_h(mapping.H, pts)
    except Exception:
        return out, ok
    finite_h = np.all(np.isfinite(h_out), axis=1)
    out[finite_h] = h_out[finite_h]
    ok[finite_h] = True
    if not mapping.use_tps or mapping.rbf is None or mapping.hull is None:
        return out, ok
    try:
        simplex = np.asarray(mapping.hull.find_simplex(pts), dtype=int)
        inside = (simplex >= 0) & finite_h
    except Exception:
        return out, ok
    if not np.any(inside):
        return out, ok
    try:
        corr = np.asarray(mapping.rbf(pts[inside]), dtype=np.float64)
        if corr.ndim != 2 or corr.shape[1] != 2 or corr.shape[0] != int(inside.sum()):
            return out, ok
        corrected = h_out[inside] + corr
        good = np.all(np.isfinite(corrected), axis=1)
        idx = np.flatnonzero(inside)[good]
        out[idx] = corrected[good]
        ok[idx] = True
        bad = np.flatnonzero(inside)[~good]
        out[bad] = pts[bad]
        ok[bad] = False
    except Exception:
        pass
    return out, ok


# --------------------------------------------------------------------------- legacy API (backward compatible)

def calculate_homography(src_pts: list, dst_pts: list) -> Optional[list]:
    """Legacy entry: returns the 3x3 H matrix (list) or None.

    Now uses MAGSAC(3.0) + LS refit internally; the lens and TPS are
    intentionally *not* included here so pure-matrix consumers keep working
    unchanged. Use fit_mapping()/apply_mapping() for full accuracy.
    """
    m = fit_mapping(src_pts, dst_pts, camera_lens=False)
    if m is None:
        return None
    return m.H.tolist()


def apply_perspective(x: float, y: float, matrix_list: list):
    """Legacy H-only point mapping (unchanged behavior)."""
    if not matrix_list:
        return x, y, False
    try:
        H = np.array(matrix_list, dtype=np.float64)
        if H.shape != (3, 3):
            H = np.asarray(matrix_list, dtype=np.float64).reshape(3, 3)
        return _h_apply_single(H, float(x), float(y))
    except Exception:
        return x, y, False


def mapping_from_matrix(matrix_list: list) -> Optional[CameraMapping]:
    """Wrap a bare H matrix (old DB rows without points) as H-only mapping."""
    try:
        H = np.asarray(matrix_list, dtype=np.float64).reshape(3, 3)
        if not np.all(np.isfinite(H)):
            return None
        return CameraMapping(
            H=H,
            inlier_mask=np.zeros(0, dtype=bool),
            src_points=np.zeros((0, 2)),
            dst_points=np.zeros((0, 2)),
            use_tps=False,
            smoothing=None,
            rbf=None,
            hull=None,
            metrics={
                "n_points": 0, "n_inliers": 0, "n_outliers": 0,
                "use_tps": False, "smoothing": None,
                "mean_error": None, "median_error": None, "p95_error": None,
                "per_point_error": [], "warnings": ["legacy matrix: no points stored"],
            },
        )
    except Exception:
        return None
