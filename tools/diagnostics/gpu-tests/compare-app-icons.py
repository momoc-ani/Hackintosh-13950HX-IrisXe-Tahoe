#!/usr/bin/env python3
"""比较 xe-app-icons 的三组真实像素，生成可检索的逐应用 HTML 与 CSV。"""
import argparse
import csv
import html
import json
from pathlib import Path

MODES = ("amd-resources", "intel8-resources", "intel4-resources")


def load_rows(root: Path, mode: str) -> dict:
    """按完整应用路径和尺寸对齐样本，避免依赖日志或执行顺序。"""
    rows = json.loads((root / mode / "results.json").read_text())
    keyed = {(row["app"], row["size"]): row for row in rows}
    if len(keyed) != len(rows):
        raise ValueError(f"{mode} 存在重复样本")
    return keyed


def pixel_difference(root: Path, mode: str, reference: dict, row: dict) -> tuple:
    """直接比较 BGRA 字节；报告平均误差和最大通道误差，不使用摘要值。"""
    expected = (root / MODES[0] / reference["pixels"]).read_bytes()
    actual = (root / mode / row["pixels"]).read_bytes()
    expected_length = reference["size"] ** 2 * 4
    if len(expected) != expected_length or len(actual) != expected_length:
        raise ValueError(f"像素长度错误: {row['app']} {row['size']}")
    if expected == actual:
        return 0.0, 0
    total, largest = 0, 0
    for left, right in zip(expected, actual):
        delta = abs(left - right)
        total += delta
        largest = max(largest, delta)
    return total / expected_length, largest


def write_html(root: Path, records: list) -> None:
    """输出本地可浏览的三列对照，保留透明背景并可按应用名筛选。"""
    body = []
    for row in records:
        pictures = []
        for mode in MODES:
            source = html.escape(f"{mode}/{row[mode + '_preview']}", quote=True)
            pictures.append(f'<td><img loading="lazy" width="96" height="96" src="{source}">'
                            f'<br>alpha {row[mode + "_alpha"]}</td>')
        name = html.escape(row["app"])
        body.append(f'<tr data-name="{name.lower()}"><td>{name}<br>{row["size"]} px</td>'
                    + "".join(pictures)
                    + f'<td>{row["intel8_status"]}<br>{row["intel4_status"]}'
                    + f'<br>4-bank max Δ={row["intel4_max_delta"]}</td></tr>')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>已安装 App 图标：Intel 8-bank / 4-bank / AMD 对照</title>
<style>body{font:14px system-ui;margin:24px;background:#f5f5f7;color:#222}table{border-collapse:collapse;width:100%}
th,td{padding:12px;border:1px solid #ccc;text-align:center}td:first-child{text-align:left;overflow-wrap:anywhere;max-width:360px}
img{object-fit:contain;background:repeating-conic-gradient(#ddd 0% 25%,#fff 0% 50%) 0/16px 16px}
input{padding:10px;width:360px;max-width:90%}thead{position:sticky;top:0;background:#fff}tr[hidden]{display:none}</style>
<h1>已安装 App 图标渲染对照</h1>
<p>应用资源直接读取；每个应用 32 / 128 / 512 px。AMD 为像素参照，4-bank 仅作用于本次测试进程。</p>
<p>PASS 要求输出非空且所有 BGRA 通道与 AMD 的误差不超过 1；没有有效参照图的样本单独保留。</p>
<input id="filter" placeholder="筛选应用名或路径"><p><a href="comparison.csv">下载逐项 CSV</a> · <a href="summary.json">查看汇总 JSON</a></p>
<table><thead><tr><th>应用 / 尺寸</th><th>AMD 参照</th><th>Intel 当前 8-bank</th><th>Intel 4-bank 对照</th><th>结果</th></tr></thead><tbody>
''' + "\n".join(body) + '''</tbody></table><script>
document.getElementById('filter').addEventListener('input',event=>{
const query=event.target.value.toLowerCase();document.querySelectorAll('tbody tr').forEach(row=>row.hidden=!row.dataset.name.includes(query));});
</script></html>'''
    (root / "index.html").write_text(document)


def main() -> None:
    """检验样本完整性，汇总真实像素差异并生成可审阅结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    root = parser.parse_args().directory
    groups = {mode: load_rows(root, mode) for mode in MODES}
    reference = groups[MODES[0]]
    for mode in MODES[1:]:
        if reference.keys() != groups[mode].keys():
            raise ValueError(f"{mode} 样本集合不完整")
    records = []
    summary = {"apps": len({key[0] for key in reference}), "samples": len(reference)}
    for key in sorted(reference):
        ref = reference[key]
        result = {"app": key[0], "size": key[1], "reference_valid": ref["output_alpha_pixels"] > 0}
        for mode in MODES:
            row = groups[mode][key]
            if row.get("input_missing") and row["output_alpha_pixels"]:
                raise ValueError(f"无输入却有输出，检查图块坐标: {row['app']} {row['size']}")
            result[mode + "_alpha"] = row["output_alpha_pixels"]
            result[mode + "_preview"] = row["preview"]
        for mode, label in ((MODES[1], "intel8"), (MODES[2], "intel4")):
            average, maximum = pixel_difference(root, mode, ref, groups[mode][key])
            result[label + "_mean_delta"] = average
            result[label + "_max_delta"] = maximum
            result[label + "_status"] = "NO_REFERENCE" if not result["reference_valid"] else (
                "PASS" if maximum <= 1 and result[mode + "_alpha"] > 0 else "FAIL")
        records.append(result)
    for mode in MODES:
        summary[mode] = {
            "input_empty": sum(row["input_alpha_pixels"] == 0 for row in groups[mode].values()),
            "output_empty": sum(row["output_alpha_pixels"] == 0 for row in groups[mode].values()),
            "input_missing": sum(bool(row.get("input_missing")) for row in groups[mode].values()),
        }
    for label in ("intel8", "intel4"):
        summary[label] = {
            "pass": sum(row[label + "_status"] == "PASS" for row in records),
            "fail": sum(row[label + "_status"] == "FAIL" for row in records),
            "no_reference": sum(row[label + "_status"] == "NO_REFERENCE" for row in records),
            "pixel_exact": sum(row[label + "_max_delta"] == 0 for row in records),
            "max_channel_delta": max(row[label + "_max_delta"] for row in records),
        }
    workspace = {}
    for mode in ("amd-workspace", "intel4-workspace"):
        if (root / mode / "results.json").exists():
            rows = load_rows(root, mode)
            workspace[mode] = {
                "samples": len(rows), "input_empty": sum(row["input_alpha_pixels"] == 0 for row in rows.values()),
                "output_empty": sum(row["output_alpha_pixels"] == 0 for row in rows.values()),
            }
    summary["workspace_current_session"] = workspace
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    with (root / "comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    write_html(root, records)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
