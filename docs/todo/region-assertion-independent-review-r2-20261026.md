# 区域多组件断言 · 独立审查准备 R2（2026-10-26）

- 编制：C 轨·回归审查（独立审查方，未写任何产品代码）
- 本轮性质：对 A 轨（快捷键框选 UI）batch 1+2 与 B 轨（条件模型/编译回放）batch 1+2 **合并状态的独立验收**
- 快照：2026-10-26T00:25:10+0800，分支 `hermes-remote`，79 条脏项（29 改跟踪文件 +3256/−185）；A/B 并发中，所有 file:line 仅供后续对照
- 结构沿用 R1（`docs/todo/region-assertion-independent-review-r1-20261026.md`）；配套回归报告 `result-report/20261026-region-assertion-regression/r2.md`

## 1. 06 §6 验收项分类与当轮覆盖状态（沿用 R1 三类标记）

| # | 验收项 | 分类 | R2 覆盖状态 |
|---|---|---|---|
| 1 | Ctrl+Alt+Q 实机弹出遮罩；Esc 恢复；不触发业务快捷键、不录伪动作 | 【RDP】+【离线】 | 离线路由/状态机已落地（ui_controller region 六命令路由、overlay 控制器）；热键真实投递 **待实机**（test_region_ui.py:665 正当 skip） |
| 2 | 多选增删、高亮与身份一致；选区/窗口失效明确失败 | 【离线】+【11.26】+【RDP】 | 离线选择/身份校验已落地；真实拖选命中与多屏/DPI **待实机** |
| 3 | 第二/三行满足通过；跨行拼接反例失败 | 【离线】 | ✅ 已验收：evaluate_region_conditions 同帧行级合取 + 逐行判定（region_assertion.py:920-958），三态语义齐备 |
| 4 | 同名行/排序/重建/空列表/虚拟列表可解释 | 【离线】+【11.26】 | 离线语义（undetermined 口径）已验收；真实长列表只查可见数据 **待实机** |
| 5 | 默认 5s/1min 显示、独立修改真实生效；边界/超时/解析失败证据 | 【离线】+【11.26】 | 离线校验通过（positive number、上限、future_time_rejected、undetermined 保原文）；真实回放阈值生效 **待实机** |
| 6 | 首匹配四反例（首满足/超期/短暂消失/整期不存在） | 【离线】+【11.26】 | ✅ 离线已验收：first_match 首采样通过；absent_full_period 到点才判+期间命中即失败+undetermined 不判 pass（expectations.py:1084-1143）；真实期限 **待实机** |
| 7 | 预览=录制=回放一致；证据/绑定/截图/hash 可追溯 | 【离线】+【11.26】 | 编译→verifier→求值器同一语义链已验收（7 处枚举同步）；真实产物链 **待实机** |
| 8 | 旧录制兼容、旧入口移除、保存失败保留表单重试不重复 | 【离线】+【11.26】 | ✅ 四处旧入口已按裁决移除（面板两按钮+浮窗 Add assertion+Shift+Ctrl/Cmd+A；MCP add_recording_assertion 保留）；requestId 幂等在位；实机目检 **待验** |

## 2. 受保护成果核验（R2 复核，快照 00:25:10）

1. `recorder_panel.py:171` `_request(self, request_name: str, **args)` 在位；新增 :174 `_request_with_id`、:372 `_request_region_cancel` 为扩展不破坏原签名——**无 name 形参冲突回归** ✓
2. `windows_pywinauto.py:659` focus_existing_window、:720 registered_app_not_found——位置与 R1 一致，未动 ✓
3. `ui_controller.py:580-581` `_ready.clear()`+`_last_error=None`、:600/:607 `recording_panel_unavailable`——行号漂移（R1 时 462-490），逻辑完整 ✓
4. 追加：session.py:275 ingest scope 过滤、:281 key 事件丢弃在位；result_store.py:161/169/187 symlink 防护未触碰 ✓
5. 旧入口四处移除落实：recorder_panel 两按钮与浮窗 "Add assertion" 文本消失；session.py:277-279 旧快捷键移除（按普通业务键处理，不支持则如实报 compile_action_unsupported，不静默丢弃）；MCP add_recording_assertion 保留（server.py:1170）✓

## 3. 文件所有权对照（R2 审计结论）

- **A 轨 10 项、B 轨 21 项、B 追加 6 项全部对上**（明细见 r2.md §3）；A/B 四份批次报告（region-ui-batch1/2、region-model-batch1/2）均存在。
- **越权/意外标注（2 项，待主 session 确认归属）**：
  1. `target/protocol_models/expectations.py`、`test_case/test_execution_unit/test_expectations.py` 被修改但不在任务书清单——内容为 region_query 注册连带，倾向归属 B 轨；
  2. `target/server.py` 的 `get_recording_capture` 签名扩展（可选 session_id）+描述变更——向后兼容但**非"既有工具零变化"**，且 R1 时 server.py 已是 M，并发下不可精确归因。
- 其余全部 M 文件均为 R1 既有 WIP，无新增越权。

## 4. 反"离线绿冒充实机"审查要点（R2 执行记录）

1. **golden 零手改** ✅（git status 全量无 golden/recordings/SHA 类文件）。
2. **不可判定不记通过** ✅：unreadable→undetermined（region_assertion.py:933/963）、bool 非真值→undetermined（:827）、value 缺失→undetermined 非 fail（:816）、provider 异常→undetermined（expectations.py:1060/1091）、absent 模式含 undetermined 采样不判 pass（:1122-1125）。
3. **离线 stub 与真机差异点（本轮仍未上机，继续标"待验"）**：
   - RegisterHotKey 真实投递：唯一 skip 即此项（test_region_ui.py:665）——A 轨未用离线 stub 冒充，**符合规则**；
   - pywinauto 真实行查询：observe_container_rows 全链路（锁链+脱敏+后复核）离线已验，真实 UIA 行为 **待 11.26**；
   - RDP 焦点：focus_existing_window 聚焦后身份复核需真实窗口重建触发，**待 11.26**。
4. **采样策略不外曝** ✅：REGION_SAMPLE_INTERVAL=0.5s 仅内部 sleep，结果 payload 无该字段。
5. **数字口径**：全量 2411/1 skipped/614 deselected、recording 866/1 skipped/1 deselected，与两轨宣称一致；每批验收继续与 baseline.md（R1）+r2.md（R2）链式对照。

## 5. 本轮验收结论

**通过（附 3 项不阻塞标注）**——详见 r2.md §6：①两处连带改动归属待确认；②get_recording_capture 非纯增量标注；③Windows/RDP 实机项（热键投递、真实行查询、焦点/多屏/DPI）下轮 11.26 上机验证，离线全绿不代替。

## 6. 下轮（R3）审查准备

- 实机项验证方法沿用 R1 §1 的真实操作步骤（不接受离线绿冒充）。
- 关注 A 轨 batch 3+（region_wizard 向导闭环、中文摘要）与 B 轨 batch 3+（若有时序断言扩展）后的枚举同步再核对（7 处清单不变）。
- 链式基线：baseline.md（R1 2263/614）→ r2.md（R2 2411/1 skipped/614）→ R3 重跑对照，增长须可归因、0 failed、skip 只认"待实机"正当标记。
