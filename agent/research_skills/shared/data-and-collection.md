# 数据与采集接口

`list_datasets` 用已知范围查目录；`open_dataset` 冻结范围与版本；`query_dataset` 读取原评；`aggregate_dataset` 确定性统计；`export_dataset` 提供只读 `/inputs` 下完整 JSONL 分片。`analysis_paths` 包含所选范围内的标准评论出现记录，包括同一实体的不同出现记录；实体计数必须去重。`raw_payload_paths` 是渠道原始记录，可能包含排除项，不能混入标准总体。默认只需标准导出；确实需要渠道专有字段时用 `include_raw=true` 补取。同一会话已有文件及冻结版本直接复用，不为读取字段或取得引用重复导出。已有关联 `job:xxx` 用 `scope={job_ids:["xxx"]}` 打开，不能把报告任务号当采集任务号。

目录返回 `catalog_id`、兼容 `batches` 和 `next_cursor`。有游标时继续分页，保留原范围或省略 scope；末页只能证明枚举结束，`catalog_complete` 表示是否有已知不可用来源，均不表示原评已读。`open_dataset` 返回 `requires_batches` 时，按同一目录的 `catalog_id + batch_id` 打开所需各批。分批不缩小用户原目标；失败、未读批次和不可用来源在最终结果中说明。`source_changed` 或 `catalog_expired` 应重取原范围目录，不重复打开失效版本或绕过验证。

跨批计数给 `aggregate_dataset` 传全部 `dataset_ids`，或已有完整导出时用本地 `research_methods.ReviewSet` 一次载入各批，在一个 Python 中组合统计和原文分析；两者按相同标准身份及顺序去重。不要逐批求和，也不要仅保留各批 rank=1 后再合并，这会丢失出现记录或错选跨批 canonical。计数单位是评论实体或出现记录，不是人数或语义主题频次。加载与引用示例见[本地方法](local-methods.md)。不同归一化版本不能直接混算，未知身份不能凭文字相似度补齐。

研究包的 `package_path` 是完整 JSON，`/datasets` 是上述导出结果列表。优先使用返回的 `schema_path`、`manifest_path`、完整结果文件及明确字段；不要递归搜索目录猜测 JSON 包装或反复试错字段。大结果的预览不等于完整数据，使用其 `full_result_file`，该文件根对象就是完整工具结果。

用户提供链接时 `resolve_product` 核实身份，`collection_capabilities` 确认渠道支持；确有必要才 `collect_product`。数据未就绪可 `wait_for_collection` 保存进度并释放运行资源，系统恢复原问题，模型不要循环轮询。每轮最多三个目标，已关联任务可复用，服务故障不通过重复采集修复。

原生 Bash/Python 是本地分析环境，无网络；不要用 requests、curl 或 pip 探测外网或调用工具 API。需要找链接且用户打开联网时，使用原生 `web_search` 同批搜索无依赖的公开产品问题，使用真实返回的完整链接；`web_fetch` 阅读相关网页。可搜索范围是原问题或可信目录中的公开产品、固定主题与支持渠道，工具会返回可选查询；不要把内部报告、原评、附件的私有内容发往外网。网页不是内部用户样本，公开评论须进入可追溯采集数据才能并入总体。

完整型号与品牌必须匹配，不能为凑数据采集相近型号；同渠道避免重复目标。更新请求应说明本轮实际采集结果与日期，复用旧数据应明确时间与缺口。联网关闭不影响已授权的数据采集采集和内部资料读取。
