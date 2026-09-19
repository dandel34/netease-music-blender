# tools/ 说明

这些脚本只在开发时用，**不影响插件运行**（`netease_music/` 里的模块不依赖它们）。

| 脚本 | 作用 | 需要 Blender | 需要联网 |
| --- | --- | --- | --- |
| `build_netease_music.py` | 由 `netease_music/` 生成 `dist/netease_music-<版本>.zip`（扩展安装包）；版本号取自 `bl_info`，manifest 一并生成，条目时间戳固定（同样内容产出同样字节） | 否 | 否 |
| `check_package.py` | 校验提交的 zip 与源码逐文件一致、manifest 字段与版本正确、包里没混进 `__pycache__` | 否 | 否 |
| `test_netease_crypto.py` | 加密自检：FIPS-197 / NIST SP 800-38A / PKCS#7 官方向量；加 `--online` 再跑一遍匿名接口连通性 | 否 | `--online` 时需要 |
| `test_netease_offline.py` | 离线单元测试（67 项）：数据标准化、私人雷达三级降级、Cookie 处理、三种协议构造、音质回退、收藏降级、**LRC 歌词解析**、**缓存上限淘汰** | 否 | 否 |
| `test_netease_client.py` | 匿名拉真实数据（每日推荐 / 歌单详情 / 播放直链 / 歌词），打印命中的协议链路 | 否 | 是 |
| `test_in_blender.py` | 真机集成测试（23 步）：真实 `addon_enable` 启用、加密、联网、下载、`aud` 播放、面板绘制冒烟、**图标名校验**、**中文歌词字体度量**、**浮层布局与注册**、**缓存上限**、收藏、自动下一首 | **是** | 部分是 |
| `test_install_in_blender.py` | 模拟「从磁盘安装」：装进临时用户目录并启用，检查面板与操作符注册 | **是** | 否 |

## 常用命令

```bash
# 改完源码 → 重新打包 → 自检
python tools/build_netease_music.py
python tools/check_package.py
python tools/test_netease_offline.py
python tools/test_netease_crypto.py

# Blender 真机测试
# 注意：脚本用真实 addon_enable 启用插件，所以要把 Blender 用户目录指到临时目录，
#       避免污染你自己的 Blender 配置和已装插件状态。
export BLENDER_USER_CONFIG="$PWD/.tmp/user_config"
export BLENDER_USER_SCRIPTS="$PWD/.tmp/user_scripts"
blender --background --factory-startup --python tools/test_in_blender.py

# 从磁盘安装测试（同样重定向用户目录）
blender --background --factory-startup --python tools/test_install_in_blender.py
```

Windows PowerShell：

```powershell
$env:BLENDER_USER_CONFIG = "$PWD\.tmp\user_config"
$env:BLENDER_USER_SCRIPTS = "$PWD\.tmp\user_scripts"
$env:TEMP = "$PWD\.tmp"; $env:TMP = "$PWD\.tmp"
& "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --background --factory-startup --python tools\test_in_blender.py
```

> 浮层的**像素级观感**没法在 `--background` 下验证（没有 GPU 上下文，`blf.draw` 会直接让 Blender 崩溃）。
> 自动化测试覆盖的是布局计算、注册/摘除与中文字体度量；实际效果请打开 Blender 看一眼。
