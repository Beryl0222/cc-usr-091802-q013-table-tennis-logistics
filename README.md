# 国际乒赛资源履约

用于协调跨城市乒乓球赛事的认证设备、物流航段和交接承诺。2027 年单打、双打与混合团体世界杯分处蒙彼利埃、澳门与成都，本服务接住赛历调整带来的履约变化，保证同一批器材不会被两个赛区同时兑现。

`fixtures/sample.json` 保存可公开的领域样例（器材档案、认证规则、技术人员名册），只用于说明数据边界，不包含真实个人资料或业务凭据。

## 运行

- `python3 service.py --check`：检查项目身份。
- `python3 service.py --port 8000`：启动服务，`/health` 返回项目标识。
- `python3 -m unittest discover -s tests -v`：核对基础契约与领域行为。

## 设计要点

- **分别留痕**：场馆时段、比赛阶段、设备序列号、认证有效期、物流航段、责任方各自建模；所有事实进入追加式台账（`fulfillment/ledger.py`），只追加、不改写。
- **赛历接收**：`POST /schedule-versions` 先归一化时区，再识别设备及技术人员的重叠承诺（赛历内部互撞 + 与账上已有承诺相撞），随后为每项需求生成带时间余量与选择理由的方案，负责人确认（`POST /proposals/{id}/confirm`）后才落承诺；确认时复核，失效选项不能确认。
- **禁直接改派**：交接或启运以后承诺锁定，`POST /commitments/{id}/reassign` 返回 409；延期、清关受阻、故障通过 `POST /incidents` 只触发相关节点的替换方案，候选设备须满足该场比赛当时有效的认证规则，确认后原承诺进入 `REPLACED` 并串入替换链。
- **唯一事实**：扫描枪离线补录凭幂等键去重，同一交接节点只认一条事实；承运人回调按回调号与业务指纹双重去重；所有时刻归一为 UTC，跨时区到达信息不会改出第二条事实。
- **准备页与还原**：`GET /matches/{id}/preparation` 给出每项资源的位置、下一承诺、风险缓冲与确认人；`GET /matches/{id}/replacement-chain` 还原完整替换链；`GET /matches/{id}/schedule-history` 与 `POST /schedule-versions/{id}/restore` 支持反向还原赛历改动（以新版本落账，不改写历史）。

## 主要端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份 |
| POST | `/schedule-versions` | 接收新版赛历，返回冲突与待确认方案 |
| GET | `/schedule-versions/{id}` | 查看某版本接收结果 |
| POST | `/schedule-versions/{id}/restore` | 以历史版本内容签发新版本 |
| POST | `/proposals/{id}/confirm` | 负责人确认方案选项，落承诺 |
| POST | `/proposals/{id}/reject` | 驳回方案 |
| POST | `/commitments/{id}/dispatch` | 登记启运（幂等） |
| POST | `/commitments/{id}/reassign` | 账面改派（启运/交接后 409） |
| POST | `/handovers` | 交接留痕（扫描枪离线补录幂等） |
| POST | `/carrier-callbacks` | 承运人回调（重复回调去重） |
| POST | `/incidents` | 延期/清关受阻/故障，生成节点替换方案 |
| GET | `/matches/{id}/preparation` | 比赛准备页投影 |
| GET | `/matches/{id}/replacement-chain` | 完整替换链 |
| GET | `/matches/{id}/schedule-history` | 赛历改动轨迹 |
| GET | `/ledger/events` | 台账事件审计 |
