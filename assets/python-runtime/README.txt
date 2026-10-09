Windows 离线运行时目录

联网部署时运行 scripts\\prepare_offline_runtime.cmd，它会下载官方 Python 3.12.10
x64 embeddable distribution 并解压到此目录。发布离线包时，将该目录连同 python.exe、
python312.dll、python312.zip 和 *.pyd 一起分发。bootstrap_windows.cmd 会优先发现
此路径；若目录没有 python.exe，则回退到系统 py/python 或 winget 当前用户安装。

不要把网络下载逻辑写入报告 HTML；报告运行时只依赖本地 Python、SQLite 和内联前端资产。
