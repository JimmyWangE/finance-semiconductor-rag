# 研究台界面

2026-09-07 按用户指定的 ui-ux-pro-max skill 更新研究台、文档知识库和 PDF 校对界面。
技能安装于 `/root/.codex/skills/ui-ux-pro-max`，来源为
[nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)。
下一轮可直接使用该 skill。其自动生成的营销/作品集页面结构不适合本项目；采用其中适用的
栅格、信息层级、键盘焦点、响应式和可访问性规则，形成独立研究工作台设计。

## 本次更新

- 研究台：会话侧栏、主对话区、证据区三栏结构，底部输入，收纳检索条件和逐轮引用摘录。
- 空会话：半导体研究主题与可直接填入的示例问题；查询时显示等待状态。
- 工具：使用本地 Lucide 图标，保留按钮名称、悬停提示与键盘焦点。
- 文档页：统一导航、真实语料统计、PDF 拖入区域、上传任务和入库研报双视图。
- 文档浏览：按标题、机构、日期搜索，15 条一页。表格在小屏内部横向滚动，不撑开页面。
- 校对：原 PDF 与识别文字对照，位置高亮、页码栏和底部操作条。
- 手机：可展开会话导航，主操作保持可见；平板与横屏按宽度重排。

## 文件

设计规范：`design-system/semi-research/MASTER.md`。
共用样式与图标：`src/semi_rag/ui/`，由 FastAPI `/ui` 路由提供。
研究台：`workbench.html`、`workbench.js`；文档页：`library.html`、`library.js`。
无新增运行时前端框架或外部 CDN 请求。Lucide 静态图标版本 1.41.0，许可证随资源保存。

## 验证

18 项现有后端测试通过。真实模型的连续追问、会话恢复/切换/删除、逐轮引用测试通过。
上传、校对修改、原文高亮像素、入库与手机校对验收通过。
响应式验证覆盖 1440×1000、1024×768、768×1024、375×812、844×390，包含 reduced-motion，
未发现横向页面溢出或脚本错误。测试脚本 `tests/browser_ui.cjs`、`tests/browser_conversations.cjs`、
`tests/browser_uploads.cjs`，截图与结果在 `data/rag/eval/ui-20260907/`。

访问研究台 `http://127.0.0.1:8090/`，文档知识库 `http://127.0.0.1:8090/library`。
