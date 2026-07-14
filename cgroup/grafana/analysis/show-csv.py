#!/usr/bin/env python3
"""
show-csv.py — 把 analyze.py 產生的 CSV(預設 cgroup_limits.csv)印成好看的表格。

純標準函式庫,不需 pandas / tabulate。欄寬對齊沿用 analyze.py 報表的寫法
(欄寬 = max(欄名長度, 該欄所有值長度)),再加上框線與數字右對齊。

用法:
  python3 show-csv.py                    # 讀 cgroup_limits.csv
  python3 show-csv.py my_limits.csv      # 讀指定檔
  python3 show-csv.py -                  # 從 stdin 讀(可接管線)
"""
import csv
import sys


def load(path):
    f = sys.stdin if path == "-" else open(path, newline="")
    with f:
        rows = list(csv.reader(f))
    if not rows:
        sys.exit("(空檔案)")
    return rows[0], rows[1:]


def render(header, rows):
    cols = range(len(header))
    # 欄寬 = max(欄名長度, 該欄所有值的長度) ← 同 analyze.py 的欄寬計算
    wid = [
        max(len(header[c]), *(len(r[c]) for r in rows)) if rows else len(header[c])
        for c in cols
    ]

    def cell(text, c):
        # 第 0 欄(user/service)靠左;其餘多為數字/帶單位 → 靠右
        return text.ljust(wid[c]) if c == 0 else text.rjust(wid[c])

    def border(left, mid, right):
        return left + mid.join("─" * (wid[c] + 2) for c in cols) + right

    def line(vals):
        return "│ " + " │ ".join(cell(vals[c], c) for c in cols) + " │"

    out = [border("┌", "┬", "┐"), line(header), border("├", "┼", "┤")]
    out += [line(r) for r in rows]
    out.append(border("└", "┴", "┘"))
    return "\n".join(out)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "cgroup_limits.csv"
    header, rows = load(path)
    print(render(header, rows))
    print(f"\n共 {len(rows)} 列  ({path})")


if __name__ == "__main__":
    main()
