# 第三方来源与许可

## tools/xhs_sign/ —— GPL-2.0

小红书签名引擎（`sign_util.py` 与 4 个 `xhs_xray*.js` / `xhs_xs_xsc*.js`）移植自
[TripStar](https://github.com/1sdv/TripStar)（GPL-2.0），其底层来自 Spider_XHS 项目。

**这意味着**：只要你分发的版本里包含 `tools/xhs_sign/` 及调用它的 `xhs_photos.py`，
整个衍生作品就受 GPL-2.0 约束 —— 分发时必须一并提供源码并沿用 GPL-2.0。

如果你希望以更宽松的许可分发本 skill，**删掉这两处即可**：

```bash
rm -rf tools/xhs_sign tools/xhs_photos.py tools/package.json
```

删除后 `build_route.py` 的 `--xhs` 参数会自动降级（`import xhs_photos` 失败时打印
「小红书补图跳过」并继续），其余功能（路线、高德照片、天气、海拔、分享页）完全不受影响。

## 其余部分

`build_route.py`、`weather.py`、`cache.py`、`make_share.py`、`share_template.html`、
`index.html` 为本 skill 原创，可按你自己的意愿授权。

## 运行时依赖的外部服务

| 服务 | 授权要求 |
|---|---|
| 高德地图 Web 服务 / JS API | 使用者需自备 key；受高德开放平台服务条款约束 |
| Open-Meteo | 非商业用途免费、免 key（[使用条款](https://open-meteo.com/en/terms)） |
| 小红书 | 无公开 API，走的是网页端接口；使用者需自备 Cookie，自行评估合规风险 |

底图与 POI 照片版权归高德及其用户所有，分享路书时请保留页面底部的来源标注。
