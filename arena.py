"""Validated shared arena coordinates in centimetres. No guessed default layout."""

import hashlib
import json
import math
import os
from pathlib import Path

import tello_net

FILE = tello_net.DATA_DIR / "arena.json"


def normalize(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("フィールド設定がありません")
    if value.get("preset") is not None:
        if value.get("preset") != "diamond8":
            raise ValueError("配置の種類が不正です")
        spacing = value.get("spacing")
        if isinstance(spacing, bool) or not isinstance(spacing, (int, float)) or not math.isfinite(spacing) or not 50 <= spacing <= 120:
            raise ValueError("パッド間隔は50〜120cmにしてください")
        spacing = round(float(spacing), 1)
        s = spacing
        # Seen from above: 2 / 1 3 / 4 5 6 / 7 8.
        # X follows the rocket arrows (A -> B); Y points across the rows.
        value = {"pads": {"1": [-s, -s/2], "2": [0, -3*s/2], "3": [s, -s/2],
                          "4": [-2*s, s/2], "5": [0, s/2], "6": [2*s, s/2],
                          "7": [-s, 3*s/2], "8": [s, 3*s/2]},
                 "field": [-3*s, 3*s, -2*s, 2*s],
                 "starts": {"A": [-2*s, s/2, 0], "B": [2*s, s/2, 180]},
                 "preset": "diamond8", "spacing": spacing}

    def point(raw, label, length=2):
        if not isinstance(raw, (list, tuple)) or len(raw) != length:
            raise ValueError(f"{label}の座標を入力してください")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               or abs(v) > 2000 for v in raw):
            raise ValueError(f"{label}の座標が不正です")
        return [round(float(v), 1) for v in raw]

    raw_pads = value.get("pads")
    if not isinstance(raw_pads, dict) or set(str(k) for k in raw_pads) != set("12345678"):
        raise ValueError("パッド1〜8をすべて登録してください")
    pads = {str(i): point(raw_pads[str(i)], f"パッド{i}") for i in range(1, 9)}
    for i in range(1, 9):
        for j in range(i + 1, 9):
            if math.dist(pads[str(i)], pads[str(j)]) < 20:
                raise ValueError(f"パッド{i}と{j}が近すぎます")
    # The user-defined pattern is 2 / 1 3 / 4 5 6 / 7 8. Distances may
    # vary, but the numbered rows and left-to-right order must stay intact.
    if not (pads["1"][0] < pads["3"][0]
            and pads["4"][0] < pads["5"][0] < pads["6"][0]
            and pads["7"][0] < pads["8"][0]):
        raise ValueError("パッド番号の左右の並びを確認してください")
    row_y = [pads["2"][1],
             (pads["1"][1] + pads["3"][1])/2,
             sum(pads[str(i)][1] for i in (4, 5, 6))/3,
             (pads["7"][1] + pads["8"][1])/2]
    if not all(row_y[i] + 15 < row_y[i+1] for i in range(3)):
        raise ValueError("パッド番号の奥から手前の並びを確認してください")

    field = point(value.get("field"), "飛行範囲", 4)
    x0, x1, y0, y1 = field
    if x1 - x0 < 100 or y1 - y0 < 100 or x1 - x0 > 1000 or y1 - y0 > 1000:
        raise ValueError("飛行範囲の幅は100〜1000cmにしてください")
    for i, (x, y) in pads.items():
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            raise ValueError(f"パッド{i}が飛行範囲の外です")

    raw_starts = value.get("starts")
    if not isinstance(raw_starts, dict) or set(raw_starts) != {"A", "B"}:
        raise ValueError("A・Bの開始位置を入力してください")
    starts = {side: point(raw_starts[side], f"{side}の開始位置", 3) for side in ("A", "B")}
    if starts["A"][2] != 0 or starts["B"][2] != 180:
        raise ValueError("Aはロケット向き0°、Bは反対向き180°にしてください")
    if starts["B"][0] - starts["A"][0] < 100:
        raise ValueError("AとBの開始位置は前後100cm以上離してください")
    for side, (x, y, _) in starts.items():
        if not (x0 + 20 <= x <= x1 - 20 and y0 + 20 <= y <= y1 - 20):
            raise ValueError(f"{side}の開始位置は飛行範囲の端から20cm以上離してください")
    for side, pad in (("A", "4"), ("B", "6")):
        if math.dist(starts[side][:2], pads[pad]) > 10:
            raise ValueError(f"{side}の開始位置はパッド{pad}の中心にしてください")
    result = {"pads": pads, "field": field, "starts": starts}
    if value.get("preset") == "diamond8":
        result.update(preset="diamond8", spacing=value["spacing"])
    return result


def fingerprint(value: dict | None) -> str | None:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16] if value else None


def load() -> dict | None:
    try:
        return normalize(json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save(value: dict) -> dict:
    result = normalize(value)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = FILE.with_name(FILE.name + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, FILE)
    return result
