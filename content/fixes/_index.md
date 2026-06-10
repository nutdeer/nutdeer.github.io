+++
title = "Fixes"
description = "Small troubleshooting notes and compact fixes."
sort_by = "date"
render = true
template = "fixes_list.html"
[[extra.fixes]]
date = 2026-05-31
title = "QGC 连接后脚本才收到 PX4 心跳"
content = "现象：运行 flow_check.py 后一直显示等待心跳包。\n处理：先打开 QGC 连接飞控，确认能正常读取数据后关闭 QGC，再运行脚本。\n备注：优先检查端口号，例如 /dev/ttyACM0。"

[[extra.fixes]]

date = 2026-05-20
title = "ROS工具"
content = "看话题\nrqt_plot 数据曲线\nrqt_bag 导入包看数据发布频率啥的\nrosrun plotjuggler PlotJuggler 数据曲线\n看日志\nrosrun swri_console swri_console"

[[extra.fixes]]

date = 2026-06-01
title = "VSCode C++ 函数太长参数太多看不清赋值给了哪个参数"
content = "下载插件`Settings Cycler`\n`ctrl+shift+p` 打开键盘快捷方式\n\n```c++\n## keybindings.json\n// 参数提示\n    {\n    \"key\": \"ctrl+shift+j\",\n    \"command\": \"settings.cycle\",\n    \"args\": {\n        \"id\": \"toggleCppParameterHints\",\n        \"values\": [\n            {\n                \"C_Cpp.inlayHints.parameterNames.enabled\": true\n            },\n            {\n                \"C_Cpp.inlayHints.parameterNames.enabled\": false\n            }\n        ]\n    },\n    \"when\": \"editorTextFocus && (editorLangId == c || editorLangId == cpp)\"\n    }\n```\n\n然后按 `ctrl+shift+j` 就可以切换了\n如果报错没效果检查一下项目目录下 `.vscode\\settings.json` 看是不是设置 inlayHints 了，会覆盖"

[[extra.fixes]]

date = 2026-06-10
title = "获取历史版本"
content = "git log --oneline\n签出\ngit checkout f5be475"
+++
