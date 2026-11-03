# 来源与交付

正文引用工具返回的完整 `[src_编号]`。报告章节引用表示报告证据，不能冒充已逐条核验原评。自主代码产生的输出用于分析，不自动成为原始评论来源。

从完整导出中选中评论时，用本地 `ReviewSet.citation(record_id)` 或导出清单的 `citation_prefix` 加实际 `record_id`，格式 `[review:ds_完整数据集哈希:记录ID]`。已读完整原文后不必再调用 `query_dataset` 取得另一种引用编号。服务会按已导出的冻结清单解析、保存原始评论来源并核对版本；不能自造 dataset_id 或把 `dsrc_` 当正文来源。尚未导出、只需少量原文时才按需批量 `query_dataset(review_ids=[...])`；指定出现记录时使用 `population="occurrences"`。

直接以 DSH 最终回答交付，无需另调提交工具。需要文件时在 `/workspace` 自主编写 Markdown、HTML、表格、图和脚本，使用 `publish_artifact` 保存私有可下载文件；返回相同内容及文件名的重试会复用已有文件。代码可用 pandas、numpy、duckdb、openpyxl 等已安装库。

需要排版 PDF 时，可在原生 Bash 使用内置通用排版命令：

```sh
python /opt/voxquarry/report_render.py --input /inputs/published-markdown-实际哈希.md --title "研究报告" --filename report --formats pdf
```

先发布 Markdown，服务会校验其中的来源并附上原文引用，返回 `portable_path` 和下载地址；有引用时用这个完整文件生成 PDF，无引用时用原工作文件。生成的 PDF 再通过 `publish_artifact` 保存。排版工具只处理文档格式，不规定研究方法或报告章节。没有 PDF 要求不必生成。PDF 失败仍交付已有正文与 Markdown。

一页简报压缩正文，避免重复长引文；附录不冒称正文页数。后续改写复用会话证据与成果，不重做原始研究。
