# Scripts

常用脚本入口统一放在 `scripts/` 下，避免根目录堆积低频命令。

- `rebuild_indexes.py`
  批量重建 `data/workspaces/` 下所有课程的索引。
- `perf/`
  性能基准、追踪与差异分析脚本。
- `eval/`
  judge、review、dataset lint 等评测脚本。

推荐命令：

```bash
py -3.11 scripts/rebuild_indexes.py
py -3.11 scripts/perf/bench_runner.py --help
py -3.11 scripts/eval/judge_runner.py --help
```
