# Optional isolated runtime / 可选隔离执行器

The ordinary workbench and report research do not need Docker. The optional harness adds autonomous collection, attached-file analysis and isolated code execution. It is an experimental integration: the source and mocked boundary tests are included; a new engine and live provider have not been certified by the initial local build.

普通工作台与报告研究无需 Docker。高级执行器用于自动采集、附件分析和隔离代码执行；首发包含实现与边界测试，但没有把模拟验证描述为真实新引擎或模型验收。

## Deployment contract / 部署约定

Use a **dedicated Linux VM/engine**, not an engine hosting other projects. Engine name must be `voxquarry` or `colima-voxquarry`; the manager pins its exact engine ID and four immutable image IDs. The initial configurator refuses default/shared contexts and nonempty engines. It builds images and writes private configuration only; it does not start containers or change the global Docker context.

建议一个专用 Linux VM，配置独立 Docker context。默认研究并发为 1。准备足够的内存、磁盘和浏览器/模型访问条件；增加并发会触发更高资源检查。不要将 Docker socket 挂载给应用、模型或 analysis 容器；只有可信 manager 持有它。

```sh
python3.11 scripts/configure_runtime.py \
  --context voxquarry \
  --broker-url http://APPLICATION_PRIVATE_ADDRESS:18763/wenquan/internal/broker \
  --output .runtime/harness
```

Replace `APPLICATION_PRIVATE_ADDRESS` with an address the relay can reach inside that private environment. This is not the browser's public URL. With a remote engine, copy the two generated configuration files to the dedicated VM using private file transfer; Docker bind mount paths must exist on the Docker host. The app's `VOXQUARRY_INTERNAL_URL` must use the same reachable service authority so the gateway permits internal Host headers. An SSH tunnel can expose the browser origin locally while keeping the service private.

需要替换为私有网络内 relay 可达的应用地址，不是任意公开域名。远程 Docker 的挂载路径必须位于该 Docker 主机。默认回环监听只服务同机调用；跨 VM 运行时需要显式配置私有监听、路由与访问控制，不能只修改 URL。应用可通过 `uvicorn voxquarry.server:create_app --factory --host <private-interface> --port 18763` 监听该私有接口。

Inspect `.runtime/harness/manager.json`. On the dedicated Docker host, start the exact `manager_image` ID from that file with these mappings:

```sh
docker --context voxquarry run -d --name voxquarry-manager \
  -p 127.0.0.1:19001:9001 \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --mount type=bind,src=/PRIVATE/CONFIG/manager.json,dst=/run/voxquarry/manager.json,readonly \
  --mount type=bind,src=/PRIVATE/CONFIG/management-token,dst=/run/voxquarry/management-token,readonly \
  sha256:REPLACE_WITH_PINNED_MANAGER_IMAGE_ID
```

The host must provide routing from the app to the manager's private endpoint. Load the management token from the private file into the application environment, not into a browser or model prompt:

```sh
export VOXQUARRY_WENQUAN_HARNESS_ENABLED=true
export VOXQUARRY_WENQUAN_MANAGER_URL=http://127.0.0.1:19001
export VOXQUARRY_WENQUAN_MANAGER_TOKEN="$(cat .runtime/harness/management-token)"
```

Run this only in your own trusted terminal; never print or commit the token. Model configuration uses the variables in [configuration.md](configuration.md). The autonomous budget guard currently uses the versioned model/provider price table in `agent/model_registry.py`; unknown model/provider prices fail closed. A generic compatible model can work for ordinary research without being accepted by the optional harness budget policy. Confirm provider/model availability and prices before using the harness; the bundled price snapshot is not a current billing quote.

自动研究有费用与运行上限。未知价格会拒绝执行，不会把未知当作免费。通用模型能用于普通报告研究，并不自动代表它符合高级执行器的预算策略。高级模型目录可通过 `VOXQUARRY_WENQUAN_MODELS_JSON` 配置已定义的模型键；没有把预设供应商和历史价格声称为当日全部可用。

## Verification / 验证

1. `make check`: offline contracts, identity, capabilities, manager/relay behavior, mocked lifecycle.
2. Inspect engine identity and immutable images. Start only the dedicated manager; it reclaims resources carrying its own managed label on restart.
3. Run the synthetic-only VM smoke inside that VM, with no active research:

```sh
WENQUAN_RUN_VM_SMOKE=1 python3.11 runtime/harness/vm_smoke.py \
  --production-guest \
  --expected-engine-id EXACT_DEDICATED_ENGINE_ID \
  --image-tag alpha \
  --output /PRIVATE/OUTPUT/voxquarry-smoke
```

The legacy `--production-guest` flag means “execute inside the dedicated Linux guest.” It does not refer to any other deployment. The smoke uses a synthetic broker, creates test containers/networks, and must never target an unrelated engine. Inspect its result before enabling real inference. Then run one small model request under a user-set cost limit and confirm the actual model receipt, source citations, saved artifacts and cleanup.

这几个步骤分别证明不同事项：契约、引擎隔离、实际模型。没有对应回执时，只记录未验证，不升级为已支持。图片/PDF 渲染在 analysis 镜像内安装固定依赖和中文字体；宿主测试跳过这些依赖不等于 Docker 渲染已通过。
