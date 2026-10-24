# 来源与许可状态

本文件记录来源和实际核查结果，不授予第三方代码的新许可。

## 直接上游

- 原用户提供地址：<https://github.com/zhengzhi-zy/travel_agent>。
- 当前重定向地址：<https://github.com/tuxin-winter/travel_agent>。
- 本地初始源码版本：[`4b9c1c71bc151c3ea2dc236f14fd84099a104ded`](https://github.com/tuxin-winter/travel_agent/tree/4b9c1c71bc151c3ea2dc236f14fd84099a104ded)。初次部署通过 GitHub 源码归档下载，未包含上游 Git 历史。
- 2026-10-24 核查：上游仓库未提供 LICENSE/COPYING/NOTICE，README 未给出明确的再分发许可。公开可访问不等于已经授予任意修改、再分发或商用权限。

因此，本仓库暂不为包含上游代码的整个项目添加 MIT 或其他自行选择的统一许可证。公开再分发前，应取得或核实覆盖实际复用内容的授权，并保留其署名、许可与修改声明。此项仍待上游明确，不能由本地测试代替。

## 学习参考

[Hello Agents](https://github.com/datawhalechina/hello-agents) 是学习参考，其主仓库的 [LICENSE.txt](https://github.com/datawhalechina/hello-agents/blob/main/LICENSE.txt) 为 CC BY-NC-SA 4.0。该许可不能自动视为对直接上游新增代码的授权；具体复用内容应分别核对来源与适用条款。

## 本地改造范围

上游已有景点/天气/酒店研究、规划、候选工具、餐饮与交通链路。本地改造包括 ROAM 页面、地点资料与交互、对话和局部编辑、锁定与撤销、排程及开放规则、独立结果验收、SQLite 任务恢复与幂等、运行统计、备份与示例、回归测试和评测文档。详细模块见 README 和 `docs/架构与取舍.md`。

高德地点照片、地图及天气信息来自各数据提供方。应用只展示实际取得的资料或历史录制结果，不拥有这些素材的版权，不为图片或接口数据授予额外授权。历史示例和运行记录已注明时间与用途。
