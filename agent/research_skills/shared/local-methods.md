# 本地研究方法

分析环境内置标准库模块 `research_methods`，可在 Bash 中 `import`，不增加远程调用、模型层或固定研究流水线。完整原评和全部字段始终可用，模型自主选择研究问题与解释；模块仅沉淀确定性读取和计算。查看接口：`python -m research_methods schema`。

## 在一次 Python 中组合

使用工具返回的真实 `package_path`；研究包根对象的 `/datasets` 是完整导出描述列表。同一目录继续准备后使用最新包，它已包含之前成功的批次，不要把多个续批包重复拼接。其他完整结果文件也用其声明路径读取，`load_result(path, pointer="/字段/子字段")` 不猜测包装，不把不存在字段当成空数据。单个 `export_dataset` 完整结果可用 `ReviewSet([load_result(full_result_file)])`。

```python
import json
from research_methods import load_result, ReviewSet

package_path = "/inputs/工具实际返回的研究包文件.json"
exports = load_result(package_path, pointer="/datasets")
with ReviewSet(exports) as reviews:
    result = {
        "coverage": reviews.coverage(),
        "rating": reviews.aggregate(group_by=["rating"]),
        "channel_rating": reviews.aggregate(group_by=["channel", "rating"]),
        "with_text": reviews.aggregate(filters={"has_text": True}),
    }
    # rows() 没有默认抽样或截断；本例显式取首条低星原文示意引用，
    # 这不是代表性抽样或语义频次。实际研究按本题需要遍历完整原文。
    example = next(reviews.rows(filters={"ratings": [1]}), None)
    if example:
        result["example"] = {"text": example["review_text"],
                             "citation": reviews.citation(example["record_id"])}
    with open("/workspace/analysis.json", "w") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
```

原评选择后用 `reviews.citation(record_id)` 返回 `[review:ds_...:occ_...]`，或 `reviews.citations(record_ids)` 一次整理引用并去掉重复条目。只接受已加载、校验过且真实存在的记录；不必再远程 query 获取引用。`rows()` 每条增加 `dataset_id` 和全局 `analysis_entity_rank`；原始 `canonical_entity_rank` 是单份导出的排名，不能拿来做跨批计数。所有原字段（含完整 `review_text`、`review_title`、`original_fields`）保留。

## 精确口径

- 默认 `population="entities"`：按标准 `review_entity_id` 全局去重；未知身份回退到各自 occurrence，不根据文字相似度合并。先选全局 canonical，再应用筛选，与 `aggregate_dataset` 一致。
- `population="occurrences"`：保留不同渠道/任务出现记录，相同 occurrence 在重叠导出中只计一次。若要比较各渠道的所有出现记录，显式选此口径；它不是人数。
- `aggregate(group_by=[...], filters={...}, query=..., population=...)` 支持与服务相同的最多三个分组维度和筛选字段；`schema()` 列出字段。`query` 只是标题与正文 casefold 子串匹配，不是语义检索或情绪分类。
- `coverage()` 区分已加载行、去重出现记录、实体和正文覆盖；跨批总量不能逐批相加。`export_complete` 仅表示已给出的分片全部校验通过，`source_complete` 保留来源完整度，均不证明用户原目标范围已全部纳入。已知应有数据集列表时传 `expected_dataset_ids=[...]` 核对缺批。

所有分片按导出 `analysis_paths` 顺序流式读取并核对 manifest、字节数、条数、哈希后才返回结果；缺片、版本冲突或未知字段报错，不以部分结果冒充完整统计。跨批需同时匹配 `normalization_version` 与 `normalization_contracts`；缺少后者的旧冻结数据可单独读取，但不能与新契约直接混算，应按同一版本重新准备。索引用临时 SQLite 保存，在 `with` 退出后释放；渠道原始 payload 不混入。相同 occurrence 的冻结内容冲突会拒绝合并，即使冲突字段不参与当前分组也不静默择一。磁盘不足时保留已有成果、说明缺口，不把未完成统计写成零。

只需一次简单命令也可用：

```sh
python -m research_methods aggregate --exports /inputs/真实研究包.json --pointer /datasets --group-by rating
```

多项计算优先合并为一次 Python 和一个 `ReviewSet`，避免多条 CLI 各自重复扫描。分析环境无网络；不要通过 Bash 安装依赖、请求公网或直连工具服务。
