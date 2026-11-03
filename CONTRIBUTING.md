# Contributing / 参与贡献

Start an issue describing the concrete user behavior and the smallest reproducible example. Keep changes bounded. Use synthetic fixtures; never include private datasets, credentials, browser cookies or raw production logs.

提交问题时说明用户操作、实际结果和最小复现。使用合成 fixture，不提交实际客户数据、密钥、Cookie 或生产日志。

Run `make check` for code changes. Preserve metric numerators/denominators, source hashes and citation ownership. Update both language surfaces for user-facing copy. New collectors need fixture coverage for missing identity, duplicate pages, drifting totals, access failures and partial preservation. A live-site success is separate evidence from a fixture test.

代码变更运行 `make check`。不要为了让测试通过而降低数据完整性或权限要求；界面变更同时维护中英文。新增连接器应分别记录离线契约测试与实网小批次结果。

Contributions are submitted under the project license. Keep third-party notices. Do not include paid-model credentials in CI; provider integration runs must be explicitly configured by the operator.
