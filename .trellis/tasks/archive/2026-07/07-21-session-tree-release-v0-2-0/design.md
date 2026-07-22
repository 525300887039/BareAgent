# 总体设计：顺序交付与发布确认边界

## 任务边界

父任务不修改产品文件。它维护两个子任务之间的硬顺序和两次不同性质的用户确认：

1. 当前规划审阅后的“实施确认”只授权启动并实施子任务一，然后按顺序实施子任务二的可逆发布候选工作。
2. 发布候选完成后的“最终发布确认”才授权创建 annotated tag、push tag 和触发正式 PyPI 发布。

## 顺序状态机

```text
planning（父 + 两个子任务）
  -> 用户实施确认
  -> child 1 in_progress
  -> child 1 验证 + 独立 work commit + Trellis 收尾
  -> child 2 in_progress
  -> 发布加固验证 + 独立 work commit
  -> push main + GitHub CI 成功 + v0.2.0 冲突复核
  -> release candidate 报告
  -> 用户最终发布确认
  -> annotated tag + push tag
  -> GitHub release workflow + PyPI + 全新环境验证
  -> child 2 / parent Trellis 收尾
```

任何阶段失败都停留在当前阶段；不得为了推进状态而降低检查强度。子任务二的依赖写入其 PRD 和实施计划，而不依赖父子目录关系隐式表达。

## 提交与元数据边界

- 子任务一工作提交只含 session tree 实现、对应测试以及确有必要的 spec 更新。
- 子任务二工作提交只含 CI/release 自动化、发布契约测试、构建/冒烟脚本、CHANGELOG 和小范围文档修正。
- Trellis archive/journal 若按项目配置产生独立 bookkeeping commits，保持在工作提交之后，不与两个交付物混合。
- 每次提交前按 Trellis Phase 3.4 展示一次文件分组；未知脏文件默认不纳入。

## 失败与回退

- 子任务一检查失败：只修改子任务一范围并重跑，不能提前开始发布工作。
- 发布候选检查失败：修复后重新运行完整本地门和 GitHub CI；不得靠手动上传绕过。
- tag push 前发现冲突：停止，不创建/移动/覆盖 tag。
- tag 已 push 后 workflow 失败：保留不可变 tag 和版本事实，诊断自动化；若 PyPI 已占用版本则使用后续新版本修复，绝不重传或移动 `v0.2.0`。

## 兼容性

- Python 版本要求、hatch-vcs 动态版本来源和 Trusted Publishing/OIDC 保持不变。
- 正常 session forest 的输出格式保持不变。
- PR/main CI 仍保持 Linux + Windows 默认测试和 Linux socket 测试；发布只复用它，不另造漂移命令集。
