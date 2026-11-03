# Configuration / 配置

## Local workspace / 本地工作区

`make start` binds to `127.0.0.1:18763`. Python 3.11 and Node 22 are the baseline. Use one application process per workspace; SQLite and the collector's process-local worker ownership are not a multi-replica deployment protocol.

`make start` 默认仅监听本机回环地址。一个工作区使用一个应用进程；不要把它当作支持多副本的数据库和任务队列部署。

| Variable | Purpose / 用途 |
|---|---|
| `VOXQUARRY_DATA_DIR` | Runtime root, default `.runtime/` / 数据与账号目录 |
| `VOXQUARRY_ORIGIN` | Exact browser origin / 浏览器访问来源，用于 Host 与 CSRF 校验 |
| `VOXQUARRY_INTERNAL_URL` | Server-to-server loopback/private URL / 模块间地址 |
| `VOXQUARRY_LLM_API_KEY` | Provider key / 模型服务密钥 |
| `VOXQUARRY_LLM_BASE_URL` | Chat-completions API base, normally ending in `/v1` / API 基址 |
| `VOXQUARRY_LLM_MODEL` | Exact provider model identifier / 实际模型标识 |
| `VOXQUARRY_WORKERS` | `false` disables background collection workers / 禁用后台采集，仅用于导入或测试 |
| `BROWSER_EXECUTABLE` | Optional browser executable; otherwise Patchright's Chromium / 可选浏览器路径 |

The program reads environment variables, not `.env` automatically. To use the supplied example, copy it to `.env`, set your values and load it in a trusted shell (`set -a; . ./.env; set +a`) before starting. Do not put secrets in shell commands saved to shared logs.

程序不自动加载 `.env`。复制 `.env.example` 后，在自己的可信终端加载环境变量再启动。首个管理员通过界面创建，没有默认密码。Cookie 使用 HttpOnly / SameSite，写操作检查来源和 CSRF，内部服务使用随机生成的凭据。默认数据目录内的 `secrets.json` 应与账号数据库一同备份。

## Import / 导入

Use Data Collection → Import JSON. See [the complete synthetic example](../examples/synthetic-reviews.json).

```json
{
  "brand": "Example brand",
  "model": "Example model",
  "category": "Lawn Mower",
  "language": "en",
  "reviews": [
    {"review_id": "example-1", "rating": 4, "review_text": "The mower is quiet and easy to store."}
  ]
}
```

Limits: 1–10,000 records, unique IDs within a file, ratings 1–5 or null, 20,000 characters per review, 20 MiB per HTTP request. `language` accepts `en`, `zh-CN` or `unknown`. Missing IDs receive import-local identifiers. Each import is a separate declared file population; importing the same file again creates another source task, so select the intended task when reporting. Imports omit reviewer names and account/profile fields. They do not automatically anonymize personal information embedded in the review body.

导入范围是文件本身，不代表整个网站。文件内 ID 必须唯一；同一个文件重复导入会产生新的来源任务，生成报告时选择正确任务。界面默认不导入评论者昵称等字段，但正文中的个人信息仍需要自行处理。中文和未知语言可以保存；首发确定性报告要求已声明的英文数据，并拦截明显超出英文规则覆盖的原文。语言检查不是完整语言识别模型。

## Collection / 采集

Walmart and Home Depot are enabled, with defaults of **3 pages, 100 records, 120 seconds of review-page collection**. UI/API limits allow 1–10 pages and 1–500 records. The configured timeout is 10–300 seconds. Product discovery and browser startup have separate bounded timeouts, so the entire task can take longer than the review-page budget.

Walmart 与 Home Depot 使用同一套限量采集契约；导入方式仍保留。其他适配器使用各自采集策略，不能把两站的页数限制外推到所有渠道。

- Each accepted page writes a checkpoint. Later timeouts, access errors or duplicate/drifting pages preserve prior valid data.
- `requested_sample_complete` means the requested cap was reached or the source was exhausted.
- `source_reconciled` requires exact, integrity-checked source reconciliation.
- `PARTIAL_SUCCESS` retains the declared total, collected count, missing count and stop reason.
- No CAPTCHA solving or access-challenge bypass is implemented. A source block is not interpreted as zero reviews.

逐页检查点保存已获得的有效评论。达到设定样本量与全量核对是不同状态；来源阻断不能被解释为零评论。实时访问条件会变化，首发测试采用合成响应；实际采集须在自己的网络环境、小批次验证。

## Models / 模型

Configure all three model variables together. Use a provider/model supporting structured JSON. For a generic compatible endpoint, provider-specific `enable_thinking` is omitted; supported provider families can use their native setting. AI outputs are saved with source identities and validation records. Failed citation verification does not publish a successful answer.

模型可能返回不满足合同的结构化内容，界面会显示失败并保留已完成记录。当前没有验证任意服务商都可即插即用。界面和回答语言独立；引用原文保持原样。提供方接收研究所需数据，费用由用户账号承担。

## Backup and network access / 备份与远程访问

For the alpha, stop the process before copying the complete runtime directory to a private backup, then restart. Restore into a separate directory and point `VOXQUARRY_DATA_DIR` at it; verify login and reports before replacing a working directory. Do not copy runtime data into GitHub.

远程使用优先通过 SSH 隧道连接回环端口。需要反向代理时配置精确 `VOXQUARRY_ORIGIN`、HTTPS 和受信任代理；不要直接暴露可首次建号的空工作区。高级 Docker 执行器有独立的网络与身份要求，见[运行指南](runtime.md)。
