+++
title = "Fixes"
description = "小型故障排除笔记与紧凑修复方案"
sort_by = "date"
template = "fixes_list.html"
[[extra.fixes]]
date = 2026-05-31
title = "QGC 连接后脚本才收到 PX4 心跳"
content = "现象：运行 flow_check.py 后一直显示等待心跳包。\n处理：先打开 QGC 连接飞控，确认能正常读取数据后关闭 QGC，再运行脚本。\n备注：优先检查端口号，例如 /dev/ttyACM0。"

[[extra.fixes]]
date = 2026-05-31
title = "Zola 主页卡片显示正文开头而不是 description"
content = "现象：文章标题下方显示正文第一段，而不是 front matter 里的 description。\n原因：主页列表模板使用了 page.summary。\n处理：列表摘要改成 page.description | default(value=page.summary)。"
+++
