# 后台执行

## BackgroundManager

<!-- src/concurrency/background.py：基于 threading 的后台任务管理器 -->

## 任务提交与通知

<!--
- submit(fn, *args)：提交可调用对象到后台线程
- drain_notifications()：获取已完成任务的结果
- 主循环每轮迭代开始时调用 drain_notifications 注入结果
-->

## NotificationManager

<!-- src/concurrency/notification.py：后台任务完成通知的存储和分发 -->
