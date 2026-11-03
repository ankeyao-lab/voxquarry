# Validation / 验证状态

Target: `v0.1.0-alpha.1`, Python 3.11 and Node 22. Tests use synthetic fixtures and mocked external services; no production system, retailer or paid model was exercised.

## Automated checks

| Suite | Run | Passed | Skipped |
|---|---:|---:|---:|
| Collection | 116 | 116 | 0 |
| Reporting | 382 | 382 | 0 |
| Research / agent | 813 | 813 | 0 |
| Local accounts | 23 | 23 | 0 |
| Products | 28 | 28 | 0 |
| Public workbench and bounded collection | 32 | 32 | 0 |
| Isolated runtime | 104 | 92 | 12 |
| **Python total** | **1498** | **1486** | **12** |
| Frontend worker | 4 | 4 | 0 |

`make check` includes all module suites, production frontend build and public-tree scanning. `make release-check` also scans tracked files. Clean-install verification uses a separate checkout of the independent repository, locked Python requirements, `npm ci`, and the same release check.

The 12 skips concern optional image/PDF/chart dependencies and fonts; they are not passes. Runtime unit tests check isolation and tool boundaries with temporary loopback endpoints, without provisioning Docker.

Integration coverage includes setup-once, login/CSRF/origin/host boundaries, forged identity rejection, internal service authorization, payload limits, explicit product-source-report links, language gating, bilingual fact invariants, immutable text, pagination and per-user research ownership. Neutral topic evidence includes all matched reviews independently of sentiment. Reporting `0.13.2` identifies this additive evidence-path change; metric values are unchanged. The blower golden fixture gains 13 topic groups/reference slots without changing extractor results or topic counts.

## Browser checks

Chinese/English reports, generation/export, product create/edit, linked reports, topic evidence/paging, small-collection forms, model-unconfigured state, settings, response-language choice and 390px navigation were exercised against the real backend. Captures contain synthetic data only. No console errors observed. [Design QA](../design-qa.md).

## Explicit boundaries

- Walmart/Home Depot: enabled with local pagination, partial-save and completeness tests. Current site access/success rates still require operator verification. No claim of full-site coverage or challenge bypass.
- AI: adapters, tools, citations and ownership checked with mocks. Real configured model inference needs environment-specific validation.
- Sandbox: configuration and unit tests pass; a fresh dedicated VM/engine/image smoke has not run. See [runtime setup](runtime.md).
- Chinese source: import/retention supported; English-rule reports reject unverified Chinese source text. Bilingual presentation does not mean Chinese deterministic-analysis parity.
- Deployment: one local process/workspace. Public hosting, multiple replicas and remote GitHub Actions have not been certified by local checks.
- Public-tree scan: detects configured secret signatures, private paths, retired brands and forbidden artifacts; not a proof of absence of all sensitive data. The independent repository contains no prior private Git history.

## 中文摘要

共运行 1,498 项 Python 测试：1,486 项通过，12 项可选渲染测试因依赖/字体缺失跳过；另有 4 项前端测试及生产构建。已验证双语界面、合成演示、产品关联、报告生成/导出、主题原文、权限边界和手机布局。独立目录采用锁定依赖重新安装并执行发布检查。

本版本为 Alpha。实网采集、真实模型推理、全新隔离引擎仍需在使用者环境验收；不以本地 fixture 结果或宣传文案替代这些外部结论。
