# tools/ 说明

这些脚本只在开发时用，**不影响插件运行**（`netease_music/` 里的模块不依赖它们）。

| 脚本 | 作用 | 需要 Blender | 需要联网 |
| --- | --- | --- | --- |
| `build_netease_music.py` | 由 `netease_music/` 生成 `dist/netease_music-<版本>.zip`（扩展安装包）；版本号取自 `bl_info`，manifest 一并生成 | 否 | 否 |
| `check_package.py` | 校验提交的 zip 与源码逐文件一致、manifest 字段与版本正确、包里没混进 `__pycache__` | 否 | 否 |
| `test_netease_crypto.py` | 加密自检：FIPS-197 / NIST SP 800-38A / PKCS#7 官方向量；加 `--online` 再跑一遍匿名接口连通性 | 否 | `--online` 时需要 |
| `test_netease_offline.py` | 离线单元测试：数据标准化、私人雷达三级降级、Cookie 处理、三种协议构造、音质回退、收藏降级 | 否 | 否 |
| `test_netease_client.py` | 匿名拉真实数据（每日推荐 / 歌单详情 / 播放直链 / 歌词），打印命中的协议链路 | 否 | 是 |
| `test_in_blender.py` | 真机集成测试：注册注销、加密、联网、下载、`aud` 播放、面板绘制冒烟、收藏、自动下一首 | **是** | 部分是 |
| `test_install_in_blender.py` | 模拟「从磁盘安装」：装进临时用户目录并启用，检查面板与操作符注册 | **是** | 否 |

## 常用命令

```bash
# 改完源码 → 重新打包 → 自检
python tools/build_netease_music.py
python tools/check_package.py
python tools/test_netease_offline.py
python tools/test_netease_crypto.py

# Blender 真机测试（把路径换成你的 Blender）
blender --background --factory-startup --python tools/test_in_blender.py

# 从磁盘安装测试（重定向用户目录，不污染真实配置）
BLENDER_USER_SCRIPTS=$PWD/.tmp/user_scripts BLENDER_USER_CONFIG=$PWD/.tmp/user_config \
  blender --background --factory-startup --python tools/test_install_in_blender.py
```

Windows PowerShell 下把 `$PWD` 换成绝对路径，并先 `$env:TEMP` 指到可写目录。
