# 13 — 轨迹、回放报告统一目录与无损减容

日期：2026-11-10。状态：设计待实施；本次不迁移、不删除、不改变执行语义。

最新统一v3待审稿：[`doc/unified-v3-trajectory-design.md`](../../doc/unified-v3-trajectory-design.md)。实施以该稿为准；本文件保留为存储盘点与分析材料。

最新用户决策见[设计14](14-three-trace-products-and-capture-first-design.md)：完整采集/首次成功优先于体积；对外只提供原子操作、原生回放、MAA三类用途，新原生产物在消费链就绪后统一v3，不平行生成golden v1/v2/v3。本文件的版本目录是兼容/历史容器示例，不是要求每次产出所有版本。冲突处以14为准。

## 1. 目标与边界

用户目标：一份轨迹的原件、v1/v2/v3 产物、各轮回放和报告都能在一个文件夹找到，减少重复存储，保持现有功能和追溯。

本片只改产物布局、路径解析、引用与无损存储，不重写录制器、编译器、执行循环或账本。不借此开放公共 v3 执行/导出门，也不把 unsupported 变成支持。

硬约束：

- 原始录制、失败与 unknown 历史不覆盖；同名录制不再复用同一个输出目录。
- raw 的 v1/v2 与 golden 的 v1/v2/v3 分别按实际 schema 标识，不能混为同一种版本号。
- 缺少某版本不自动生成一份冒名副本；转换必须确实受支持并记录来源、工具版本和结果。
- 每个 run/step/attempt 只有一个权威记录，前步使用稳定引用，不重复嵌入完整前一步。
- 不删除必要视觉定位模板，不降采样、不改 PNG 原始字节，不强制每步截图。
- 相同图片内容可以共用物理字节，但本次观测的 run/window/attempt、采集时点、脱敏元数据不能合并。命中内容 hash 不能代替本次实际采集。

## 2. 现状与占用基线

核查仓库：`/Users/edr-test/ai-projects/edr-wd`，代码快照 `e8afe18`。

| 已盘点目录 | 文件数 | 逻辑字节 | 目录内部完全相同的重复字节 |
| --- | ---: | ---: | ---: |
| `recordings/` | 505 | 24,218,195 | 11,401,020 |
| `result-report/` | 6,353 | 194,708,824 | 40,596,095 |
| `tmp/` | 352 | 3,105,309 | 215,852 |

三个目录合并按文件 SHA256 精确去重：总逻辑字节 222,032,328（约 211.7 MiB），重复字节上限 53,915,957（约 51.4 MiB，24.3%）。其中重复 PNG 32,093,397 字节，重复 diff 11,561,697 字节，重复 JSON 8,083,416 字节。

这是扫描集合的理论上限，不是承诺可直接释放的空间：其中有独立审计、实验和测试材料，不能全部当作某一轨迹的可删副本。硬链接/文件系统压缩也会影响物理占用，验收须同时报告逻辑大小与实际分配块数。

另有 `/Users/edr-test/Desktop/edr-wd-record/`，`du -sk` 为 15,200 KiB，未包含在上述去重统计。Windows 端持久结果也未纳入本机统计，不能据此声称全环境节省量。

实际入口分散：

- `agent/recording/artifacts.py`：`output_root/safe_test_name(name)` 下同时写 raw/case/golden/compile-report/pytest/MAA；同名路径可能复用。
- `agent/recording/collection.py`：已有真实收集链，应继续复用。
- `agent/e2e_report.py`：默认写桌面 `edr-wd-record/result-report/<时间>/`，默认生成 JSON/Markdown/HTML。
- `agent/trace/runs.py`：已有 `RunContext`/manifest/attempt 布局，不另造第二本运行账。
- `target/artifacts.py`：截图默认写桌面根的 `artifacts/screenshots/`。
- `target/recording/result_store.py`：目标机按 session 持久 raw/captures，收集后只标记、不自动删除。
- 受控 v3 与故障排查还会在 `result-report/`、`tmp/` 保存多份完整响应、轨迹和报告。

## 3. 一份轨迹一个文件夹

默认复用现有 `EDR_WD_RECORD_DIR` 根，不再增加另一个默认根：

```text
<EDR_WD_RECORD_DIR>/                    # 默认 ~/Desktop/edr-wd-record
  trajectories/
    TRJ-<uuid>/                        # 稳定ID；名称仅作显示，不作唯一键
      manifest.json                    # 版本、来源、路径、hash、状态、关联ID
      README.md                        # 人看的入口：轨迹版本/每次运行/报告链接
      sources/
        REC-<sessionId>/
          recording.v1.json            # 或 recording.v2.json，按真实schema
          stop-receipt.json            # 停止/持久化回执，rawRef而非再内嵌raw
          collection.json              # 持久读回、逐帧校验与收集结果
      compilations/
        CMP-<uuid>/
          golden.v1.json               # 或 .v2.json/.v3.json；只放本次实际产物
          compile-report.json
          provenance.json              # rawRef、编译参数、代码版本、schema
      runs/
        RUN-<uuid>/
          manifest.json
          ledger/                      # 保留既有账本格式/恢复语义
          summary.json                 # 计数、结果及稳定refs，不复制全量步骤
          report.html                  # 默认展示本步特征→匹配→动作→断言→结果
      recovery/
        JOB-<uuid>/                     # 旧源→修复候选→新录制→新run引用
      assets/
        sha256/<前2位>/<完整hash>.png    # 本轨迹内相同字节只存一次
      exports/
        EXP-<uuid>/                    # 仅显式导出才生成pytest/MAA/兼容包
```

同一次轨迹可以有多个原始 session、编译和 run，但不是每次都复制整个文件夹。新 session、CMP、RUN、JOB 均生成新 ID，保留 parent refs；不能用内容 hash 代替执行 attempt ID。

所有运行所需路径和报告链接均在这个 TRJ 文件夹内部，以相对路径记录。整夹复制到另一台机器仍可读、可校验、可在能力满足时回放；不要求依赖原机器绝对路径或仓库外的截图。

`README.md` 明确列出已有 raw/golden schema 及文件链接；未生成版本显示“无此产物/转换未支持”，不制造 v1/v2/v3 三份同内容文件。界面路径全部由 manifest 提供，不在 UI/报告/CLI 中分别拼接。

## 4. Manifest 与唯一事实源

新增的是容器协议，例如 `edr.trajectory-bundle/v1`，不是替换 raw/golden schema。

Manifest 至少包含：trajectoryId、displayName、bundleSchema、sources、compilations、runs、recoveries、objects。每条引用含 ID、相对路径、内容 SHA256、字节数、实际内容 schema 和 parent refs。源 hash 保留源字节口径；既有 canonical hash 另记，不能混用。

- 原 raw 文件按原字节保留，不能为了去重删控件快照、重写截图引用或修改字段白名单。
- `case.json` 是可再生投影：有消费者依赖时仍可通过既有路径对象按需物化；不能先删后发现生成器要读它。
- 一个 attempt 的本步数据以既有账本为权威，`previousAttemptId` 指向同一记录。summary 只做投影，before/after 不合并。
- HTML 是可再生人读视图，不是另一份独立断言账。默认不再同时生成三个全量报告格式；旧消费者要求的 JSON/MD 由兼容导出提供。
- HTML 的图片使用本夹相对资源链接，不内嵌 base64；本地直接打开也能看到静态内容，不依赖浏览器 file:// fetch JSON。

## 5. 减容优先级

### P1：先去掉能证明的重复写入

1. 同一源录制仅存一份；stop/collect/probe 的结构化回执引用该源，不反复持久完整 `recording` payload。错误码、阶段、原件 hash、来源关系仍保留。
2. PNG 按已校验的脱敏内容 hash 在本 TRJ 内只写一次；CAP/OBS 各自元数据保留，版本、run、报告都引用同一物理资源。
3. 视觉模板仍保存必需的 element/context 裁剪；只对字节完全相同者去重，不把整张帧当成可替代模板。
4. pytest、conftest、pytest.ini、MAA trace 不再每次编译自动重复导出；改为显式导出或消费者首次请求。unsupported 语义和已有导出契约保持。
5. 报告默认精简展示，不复制前步完整数据、不反复生成完整诊断响应、diff 和 snapshot 副本；同一代码快照存一次并引用。

不通过“少记录真实动作/删除失败历史/降低图像质量”省空间。

### P2：跨文件夹共享物理字节（可选）

初期不建设强制全局对象服务。需要进一步省空间时，可对不可变、已校验且同一文件系统的相同对象使用硬链接；路径仍实际存在于各 TRJ 内，单夹复制不会缺依赖。不支持硬链接、跨卷、权限不足时正常复制，不影响功能。禁止把可变 manifest/ledger/raw 工作文件直接互相硬链接，禁止修改一个链接污染其他版本。

统计必须按 inode/分配块核实释放空间，不用“重复文件数”冒充节省量。

### P3：冷归档与清理（单独显式操作）

稳定结束的 bundle 可显式导出标准 ZIP 无损归档；内含相对路径、manifest、源字节 hash，可恢复后校验。热数据和正在运行/收集的数据不压缩。PNG 不承诺有明显压缩收益，JSON/日志可无损压缩。

没有默认自动删除和默认保留天数。用户确认清理规则后，才允许引用可达性扫描→dry-run→可恢复隔离→确认删除。未收集目标机结果、运行中、unknown、待审计证据、源与恢复链根节点一律保护。跨机器的目标持久副本不能仅凭 Agent 本地存在文件就删除。

## 6. 最小实现接线

新增一个轻量 `ArtifactLayout`/resolver 管理路径和 manifest；不新建录制/编译/回放循环。

| 现有组件 | 最小改动 |
| --- | --- |
| `agent/recording/artifacts.py` | 使用稳定 TRJ/REC/CMP 路径；保留 RecordingArtifacts 路径属性；可选导出移出默认热路径 |
| `agent/recording/collection.py` | 继续原收集协议、hash与失败处理，只注入统一布局/对象写入器 |
| `agent/e2e_report.py` | 关联轨迹时写其 runs/RUN；非轨迹 E2E 保留独立 run，不强造轨迹来源 |
| `agent/trace/runs.py`、v3 ledger | 既有 RunContext/账本整体嵌入 runs，不改事件格式和 unknown/重启行为 |
| CLI/UI/recovery | 所有入口拿同一个路径对象；终端统一打印 bundle/source/golden/report 路径 |
| target result store | 保持现有 REC 持久契约；Agent 收集完成后只登记关联与确认状态 |

配置优先级：显式 output-root → 既有 `EDR_WD_RECORD_DIR` → 现有桌面默认根。旧 `EDR_WD_E2E_REPORT_ROOT`/`EDR_WD_ARTIFACT_DIR` 等显式分根配置保留 legacy 模式；新统一模式遇到冲突要提示/拒绝歧义，不能静默忽略用户配置或继续拆散路径。

新布局先作为 opt-in 验证，再切默认。老目录继续可读；目录输入可根据 manifest 识别新布局，无 manifest 时走原 loader。不能仅改文件位置却遗漏视觉 selector 的 `base_dir`、模板相对路径、生成 pytest 的 fixture、HTML链接、重启重建或导出读取路径。

所有相对路径拒绝绝对路径、`..` 越界和植入 symlink；不为布局方便取消结果存储的路径保护。外部导入包在安全临时目录校验后再发布，不直接任意解压到工作树。

## 7. 历史迁移

1. 只读扫描所有配置根，先生成路径/大小/hash/重复/引用关系清单；扫描错误、外部链接和缺失资产单列。
2. 用户选定集合后，以原 session/schema/run/内容 hash 建立导入映射，不能仅按目录名判为同一轨迹。
3. 先复制到 staging，重建新容器引用，不改旧 raw 与账本；生成 oldPath→newRef 映射。
4. 逐文件 hash、schema 双读、视觉资源/报告链接/账本重建/导出验证通过后，原子发布 bundle。
5. 旧位置先保留兼容索引和迁移回执。再次导入幂等；冲突产生新版本，不覆盖。删除旧副本另行确认。

历史审计目录不自动并入业务轨迹：审计自己的时间线仍独立，只引用 bundle。不得为了省空间改写已验收报告、0046原件或未知归属 WIP。

## 8. 分片与验收

前置：先修 transport 审核问题，不把存储改造和 CLI 自动路由/窗口枚举新功能塞在同一片。

- S1：统一路径、manifest、稳定ID、同名不覆盖；先不迁移旧文件。
- S2：接原收集/编译/运行报告；保留所有支持版本与消费者；新录制结束直接给单夹入口。
- S3：PNG/重复payload无损去重、按需导出、空间统计。
- S4：历史 dry-run/幂等导入/校验与便携导出；清理仍需另行确认。

必须验证：

1. raw v1/v2、golden v1/v2/v3 的现有支持范围不回退；缺能力仍在 GUI 前拒绝，不能让存储变化冒充 v3 软件验收。
2. 单/跨窗、语义/视觉、相同/不同图片、无图特征报告、重录 parent 关系、失败/unknown/cleanup 全覆盖。
3. 同名并行录制不覆盖；同一录制多次编译、同一 golden 多次回放只有新 CMP/RUN，不全量复制源。
4. bundle 整夹复制到另一个绝对路径后，资源定位、报告链接、只读重建、可支持的回放与导出正常。
5. 写盘中断/磁盘满/对象 hash 冲突/坏 ZIP/越界路径/引用缺失不产生“成功”清单，不损坏旧文件；动作前失败0GUI、动作后失败unknown语义保持。
6. 使用同一固定样本集比较逻辑字节、物理分配、对象数和读写耗时；完全重复样本的物理重复被消除，不同内容一字节不丢。总体节省率以实测为准，不先承诺24.3%。
7. 固定快照跑原两道完整pytest及显式需求/legacy回归，完整输出留存；报告与资源引用校验通过。实现本片不授权Windows部署或任何历史清理。

## 9. 给 CC 的接续边界

transport 前置修复清单（不依赖实机原件即可复现）：派发响应丢失必须走原unknown持久路径、runId/绑定token与成功end清理、多余位置参数拒绝且read_only完整透传、root不能绕过进程/角色约束、自包含回归不能读取被忽略的result-report文件。优先复用已有remote_server_tools的确定性与unknown处理，不再新增第三套载体。当前8测试通过不等于载体等价。

先处理 `result-report/reviews/20261110-transport-storage-review/independent-review.md` 的精确反例，再按 S1→S4 每片实现、测试和交付。采用现有服务/收集/编译/执行/账本，不新增重复执行链。不扫描后直接删文件、不提交截图与凭据、不重写旧录制。不新建自动化/会话/子代理，不自行提交推送或部署；需要这些动作另按本聊天授权执行。
