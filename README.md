# corpuslake

本地文本语料入湖与近重复复核工具。纯 Python 标准库（`sqlite3` + `http.server`），
不调用任何外部模型或服务，词切分、短语切片、候选过滤与相似度全部可解释、可复算。

## 快速开始

```bash
python3 run.py demo --reset      # 生成演示语料、建索引、分组
python3 run.py serve --port 8765 # 打开 http://127.0.0.1:8765
```

演示语料里有一组刻意构造的**非传递链** A~B~C（A 与 C 低于阈值），一份逐字相同的文本，
中英混排、短文本、空文本和带噪重复；侧栏可以直接看到分组里的“洞”。

常用命令：

```bash
python3 run.py import ./corpus                 # .txt/.md/.rst/.jsonl/.ndjson/.csv/.json
python3 run.py import ./corpus --db data/x.db
python3 run.py index --threshold 0.55 --phrase-n 3
python3 run.py index --mode approx             # 近似模式，明确标注、不承诺完整
python3 run.py verify                          # 全量两两校验（大语料自动确定性抽样）
python3 run.py verify --full                   # 强制全量
python3 run.py groups                          # 重建分组
python3 run.py export --format bundle --out out.zip
python3 run.py docs / tasks / rules
```

## 数据流

1. **入湖**：每篇文档保留原文与独立 ID，永不合并。逐字相同的再次上传返回 `409`，
   要求显式选择“指向已有文档 / 另存为独立身份 / 标记 duplicate_of”。
2. **版本化规则**：切词 `corpuslake.tokenizer.v1`（NFKC、小写、按类别分 token，
   每个 CJK 字符是独立 token）与短语切片 `corpuslake.phrases.v1`（句内连续 n-gram，
   去重成集合并记录出现次数）。规则集指纹 SHA-256 存在每次索引任务上。
   规则改变 → 新指纹 → 新 revision、新词表、新任务；旧索引不会被拿来判断新文档。
3. **全局词序**：快照内按文档频率升序、再按词排序，生成词表摘要（`vocab_digest`）。
4. **候选**：精确模式使用 AllPairs 前缀索引（前缀长度 `|S| - ceil(t·|S|) + 1`）
   加长度下界 `min/max ≥ t`，两者都无损；逐字相同文本额外以 content hash 成边。
   近似模式使用 MinHash + 分带 LSH，每条边 provenance 为 `approx-candidate`。
5. **精确打分**：所有候选用完整短语集合重算 Jaccard（交集/并集），只有 ≥ 阈值才成为边。
6. **分组**：对“已接受”的边取连通分量。分组**不是团**：页面与导出都列出组内每一对，
   没有直接边的一对显示为 `edge: null` 并注明“无直接边”，不会暗示任意两篇都超阈值。
7. **撤回/删除/改判**：文档可撤回（保留原文与 ID），边作为历史证据保留；重建分组时
   报告拆分、合并、解散，并可能拆成多个新组。复核决定按 `(task_id, doc_a, doc_b)` 存，
   不会串到别的任务。
8. **导出**：`manifest.json`（含全文、规则、快照、完整性声明）、`edges.json/csv`、
   `groups.json`（含 pair matrix）、`links.nq`、`documents.csv`、`rules.json`。

## 语义要点

- **非传递**：`similarity_semantics.not_transitive = true`；组是“边的连通分量”，
  不是“组内任意两篇都相似”。
- **空文档**：接受入湖，`token_count = 0`，不参与相似边；第二篇空文本需要显式身份决定。
- **重复短语**：短语集合去重，出现次数单独记录，重复不会抬高 Jaccard。
- **逐字相同**：独立身份 + `jaccard = 1.0` 的边，`candidate_reason = identical-hash`。
- **完整性声明**：精确模式声明“快照内每对都比较过”；近似模式只声明“已存边正确”，
  不声明找全。
- **断点续跑**：每篇文档一个提交点，停止后任务为 `stopped`，再次运行从已提交
  revision 继续；服务重启会把 `running` 标为 `stopped` 并保留进度。
- **快照滞后**：任务完成后新上传的文档不在旧快照内，接口与页面会显示
  `snapshot_current = false`，校验也会点名缺少 revision 的文档。

## 校验

`verify` 用独立路径（直接读 revision / 原文，不经过候选过滤）重算全量两两 Jaccard，
报告漏召、多余边、分数不一致与缺失 revision。超过 400 篇时默认对**任务哈希确定性
抽样**的 400 篇做样本内全量比较，报告 `scope = "sample"` 且明确说明样本外未校验；
`--full` / 页面按钮可对全部文档做全量比较。

## 测试

```bash
python3 -m tests.run_all     # 36 个 Python 测试（单元 + 集成 + 真实 HTTP）
node tests/ui_smoke.js       # 14 个前端纯函数检查
```

## 目录

```
corpuslake/  textrules.py 切词与短语规则      indexing.py 候选/精确打分/可续跑 worker
             store.py     schema 与文档身份    grouping.py 连通分量/拆分重建
             similarity.py 并排对照            verification.py 全量校验
             app.py       HTTP API + 静态页    static/ SPA（无外部依赖）
             ingest.py    多格式导入          exports.py 清单与边导出
             demo.py      演示数据            cli.py / run.py 命令行
tests/       harness + 三个测试模块 + UI smoke
```
