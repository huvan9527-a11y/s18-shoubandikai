# 盘后流程与投递核验

北京时间16:50启动预选；17:00状态任务独立读取已经落盘的当天结果。扫描未完成、数据未齐或异常时发“待完成/待披露”，不能发“无信号”。状态消息记录实际检查时间，延迟启动不能冒充17:00已送达。

## 独立职责

- 预选工作流只扫描并持久化结果，不等17:00、不调用成交记账。
- postclose.yml在17:00运行首报；预选工作流完成的workflow_run事件触发变化补充。读取main的已保存结果，不重新扫描。
- 以日期、状态和候选内容去重，推送成功确认后才记录accepted。网络失败、未配置密钥、额度拦截分别留痕，不冒充送达。
- longhu的ledger.yml独立在20:20/21:20运行原模拟账本逻辑；S18原开盘和收盘捕获任务不改变。
- S18默认仅扫描最近一个已收盘交易日；历史排查仍可手动传dates。

## 独立调度与兜底

已配置ChatGPT两项独立定时任务：工作日16:50核查并请求启动预选；17:00读取结果并向用户报告，同时核查GitHub状态通知任务。无需向仓库新增GitHub Token。GitHub cron保留为备用。两种云调度均非硬实时系统，必须以实际启动、检查及投递时间核验；不能承诺秒级准点。

17:00的ChatGPT状态通知独立于Server酱。Server酱密钥通过仓库Secrets配置，兼容SERVERCHAN_SENDKEY、SERVERCHAN_KEY、SERVER_CHAN_SENDKEY、SCKEY、SENDKEY。额度用尽仍可能投递失败，失败不能靠重跑绕过。需要更多额度时由密钥持有人调整服务配额。

## 审计文件

- outputs/postclose-YYYY-MM-DD.json / .md：当天状态与实际检查时间。
- state/postclose_notifications.json：每次投递尝试、服务返回码、成功确认摘要。
- S18：outputs/signals-YYYY-MM-DD.json的next_session是下一交易日候选；opening_candidates是当日历史开盘候选，不能混用。
- longhu：reports/screening_status.json中的provisional仅代表已验证披露内初筛；0只不代表最终全市场无信号。

任何通知流程都不得创建订单、修改交易条件、回放或补造模拟成交。

