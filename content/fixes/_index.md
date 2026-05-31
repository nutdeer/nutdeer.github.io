+++
title = "Fixes"
description = "小故障记录与小方案"
sort_by = "date"
template = "fixes_list.html"
[[extra.fixes]]
date = 2026-05-31
title = "QGC 连接后脚本才收到 PX4 心跳"
content = "现象：运行 flow_check.py 后一直显示等待心跳包。\n处理：先打开 QGC 连接飞控，确认能正常读取数据后关闭 QGC，再运行脚本。\n备注：优先检查端口号，例如 /dev/ttyACM0。"

[[extra.fixes]]

date = 2026-05-20
title = "ROS工具"
content = "看话题\nrqt_plot 数据曲线\nrqt_bag 导入包看数据发布频率啥的\nrosrun plotjuggler PlotJuggler 数据曲线\n看日志\nrosrun swri_console swri_console"
+++
