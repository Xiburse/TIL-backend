# 后端接口文档

后端是一个**纯机器接口** —— 没有人类可读输出。Tauri 侧只要把 stdin/stdout
原样透传即可,Rust 代码可以非常薄。

---

## 一、调用方式

```bash
echo '{"cmd":"search","args":{"tags_all":["1girl"],"limit":50}}' | python -m backend
```

| | |
|---|---|
| **请求** | 从 **stdin** 读**一行** JSON |
| **响应** | 往 **stdout** 写**一行或多行** JSON |
| **错误详情** | traceback 走 **stderr**(stdout 永远是纯 JSON) |
| **进程** | 一次调用一个进程,不需要常驻守护进程 |
| **退出码** | `0` 成功 / `1` 执行出错 / `2` 请求本身有问题 |

**配置路径**(可选,给装成应用后用):

```bash
echo '{"cmd":"status"}' | python -m backend --config "C:\Users\X\AppData\Roaming\imgshelf\config.toml"
```

---

## 一·五、id 是什么(**先看这一节**)

**所有 id 都是「磁盘上的名字」本身,不是数据库里的数字。**

| | 是什么 | 例子 |
|---|---|---|
| **图片 id** | 归档文件名的主干(不含扩展名) | `20260928-044002_20260928-044002_c772aa6ea37f` |
| **合集 id** | 合集文件夹名 | `20260928-205303_a174e15fa7d6` |

对应 `library/2026-09-28/20260928-044002_20260928-044002_c772aa6ea37f.jpg`
这张图,它的 id 就是文件名去掉 `.jpg`。合集同理 —— 文件夹叫什么,id 就是什么。

**为什么不用数据库自增主键:** 它由 SQLite 分配。换台设备重建数据库,同一个
合集/图片拿到的数字就变了 —— 同一个 `coll_id: "5"` 在 A 机器上指 cats、
在 B 机器上可能指 dogs。删除是不可逆的,这种错位不能接受。

文件名/文件夹名则是在磁盘上**看得见**、跨设备**稳定**、且天然唯一的。
这个约定在 `journal_*.jsonl` 里同样成立 —— 每行的 `image_id` 就是该图
文件名的主干,值可以直接和磁盘上的名字对得上。
所以:

- 响应里**不返回**数据库主键
- 所有参数只接受 `image_id` / `coll_id`

---

## 二、统一信封

**所有命令共用同一套请求与响应结构**,前端不需要为每条命令记不同的形状。

### 请求

```jsonc
{
  "cmd": "search",          // 必填,命令名
  "args": { ... }           // 可选,参数。所有命令共用一张参数表,缺的用默认值
}
```

### 响应

```jsonc
// 进度 / 日志 / 告警(只有流式命令会产生)
{"t":"event", "event":"image_done", "data":{...}}

// 最后一行,永远是它
{"t":"result","ok":true,  "data":{...}}
{"t":"result","ok":false, "error":{"code":"...","message":"..."}}
```

> **读法:一直读行,直到 `t == "result"`。**
> 非流式命令只产生一行 result;流式命令在它之前有若干 event。
> **前端不需要知道哪条命令是流式的 —— 读法完全一样。**

Rust 侧的最小实现:

```rust
// 伪代码,大意如此
let mut child = Command::new("python").args(["-m","backend"])
    .stdin(piped).stdout(piped).stderr(piped).spawn()?;
child.stdin.write_all(serde_json::to_string(&req)?.as_bytes())?;
for line in BufReader::new(child.stdout).lines() {
    let v: serde_json::Value = serde_json::from_str(&line?)?;
    match v["t"].as_str() {
        Some("event")  => app.emit("backend://event", &v)?,   // 转发给前端
        Some("result") => return Ok(v),                        // 结束
        _ => {}
    }
}
```

**两个必须遵守的点:**

1. **stdout 要在独立线程里持续读。** 读取被 UI 卡住 → 管道缓冲写满 →
   **后端会阻塞在写 stdout 上**,整个归档停下来。
2. **stderr 也要读。** 后端崩溃时原因在 stderr,不读就只能看到"卡住"。

---

## 三、命令一览

| cmd | 流式 | 作用 |
|---|---|---|
| `status` | | 库的整体状态。前端启动时先调它 |
| `search` | | 按 tag / 分级 / 日期 / 合集查图 |
| `image_detail` | | 单张图的完整 tag 列表(含置信度) |
| `top_tags` | | 标签频次排行 |
| `collections` | | 合集树 |
| `collection_tags` | | 某个合集的 tag 频次表 |
| `delete` | ✅ | 删除图片 / 合集(**不可逆**) |
| `ingest` | ✅ | 处理 `inbox/` |
| `reindex` | ✅ | 从 `library/` 重建索引(**不需要模型**) |
| `verify` | ✅ | 巡检 `library/` 结构完整性 |
| `check` | ✅ | 自检:GPU 加速 + XMP 段链往返 |

---

## 四、参数表(所有命令共用)

所有键都是可选的,没传就用默认值。这样 Rust 侧只要构造一个 `serde_json::Value`
就能调任何命令。

| 键 | 类型 | 默认 | 用于 |
|---|---|---|---|
| `limit` | int\|null | `null` | 分页;`ingest` 时按**顶层条目**计数(一个合集算一项) |
| `offset` | int | `0` | 保留 |
| `dry_run` | bool | `false` | `ingest`:只推理不落盘 |
| `cpu` | bool | `false` | `ingest`:强制 CPU |
| `device` | int | `0` | `ingest`:GPU 设备号 |
| `no_xmp` | bool | `false` | `ingest`:只写 `_tags.json` 边车 |
| `prune` | bool | `false` | `reindex`:删除 library 里已不存在的行。**需要 `yes`** |
| `from_images` | bool | `false` | `reindex`:忽略 journal,逐图读 XMP |
| `yes` | bool | `false` | 跳过确认(占位符下载、prune) |
| `general_threshold` | float\|null | 配置文件 | `ingest`:普通标签阈值 |
| `character_threshold` | float\|null | 配置文件 | `ingest`:角色名阈值 |
| `tags_all` | string[]\|null | `null` | `search`:全部命中(AND) |
| `tags_any` | string[]\|null | `null` | `search`:任一命中(OR) |
| `rating` | string\|null | `null` | `search`:`general`/`sensitive`/`questionable`/`explicit` |
| `date` | string\|null | `null` | `search`:**入库日** `YYYY-MM-DD` |
| `shot_from` / `shot_to` | string\|null | `null` | `search`:拍摄时间区间 |
| `min_conf` | float | `0.0` | `search` / `top_tags`:置信度下限 |
| `coll_id` | string\|null | `null` | `search` / `collection_tags`:合集 id(文件夹名) |
| `collection_key` | string\|null | `null` | `search`:合集名(大小写不敏感) |
| `image_id` | string\|null | `null` | `image_detail`:图片 id(文件名主干) |
| `name` | string\|null | `null` | `collection_tags`:合集名 |
| `tag` | string\|null | `null` | `collections`:只列含该 tag 的合集 |
| `min_freq` | float\|null | `null` | `collections`:最低出现频率(配合 `tag`) |
| `image_ids` | string[]\|null | `null` | `delete`:图片 id 数组 |
| `coll_ids` | string[]\|null | `null` | `delete`:合集 id 数组(文件夹名) |

---

## 五、数据结构

### image 对象

凡是返回图片的命令(`search` / `image_detail`)都用这一个形状。

```jsonc
{
  "image_id": "20260928-044002_20260928-044002_c772aa6ea37f",  // 文件名主干
  "rel_path": "library/2026-09-28/xxx/20260928-....jpg",  // 相对项目根
  "abs_path": "E:\\Images\\library\\...",                  // 绝对路径,直接给 <img src>
  "filename": "20260928-032352_..._563ec63448b2.jpg",
  "origin_name": "photo_2026-09-28_03-23-52.jpg",          // 入库前的文件名
  "mtime": "2026-09-28T03:23:52",                          // 图片修改时间
  "ctime": "2026-09-28T03:23:52",                          // 文件创建时间
  "shot_at": null,                                          // EXIF 拍摄时间,可能为 null
  "width": 832, "height": 1216,
  "rating": "questionable",                                 // 四个分级之一
  "rating_score": 0.644,
  "tag_count": 72,                                          // 过阈值的 tag 数
  "prompt": "1girl, solo, long_hair, ...",                  // 逗号分隔,下划线已转空格
  "collection_id": 1,  // 内部外键,不对外暴露;要定位合集用 coll_id
  "xmp_ok": 1                                               // 1=tag 嵌在图片里 0=在边车
}
```

### collection 对象

```jsonc
{
  "coll_id": "20260928-205303_a174e15fa7d6", // == 目录名。**这就是合集的 id**
  "name": "26.9.28 tg 图集",                  // 用户起的原名
  "side": 0,                                 // 0=根;1/2/3=父下的第 N 个子合集
  "parent_coll_id": null,                    // 父的 coll_id
  "depth": 0,
  "dir_rel_path": "library/2026-09-28/20260928-205303_a174e15fa7d6",
  "date_dir": "2026-09-28",
  "image_count": 10
}
```

> 没有 `id` 字段 —— 数据库自增主键不对外暴露。

### tag 对象

```jsonc
{"tag": "long_hair", "category": 0, "confidence": 0.9912, "passed": true}
```

> `tag` 里是**下划线原形**,不是显示用的空格形式。查询时也用下划线形式。

---

## 六、各命令详解

### `status`

前端启动时第一个调用。用它决定显示"空库引导"还是"图库"。

```jsonc
{"cmd":"status"}
```

```jsonc
{"t":"result","ok":true,"data":{
  "db_exists": true,
  "root": "E:\\Images",
  "library": "E:\\Images\\library",
  "inbox": "E:\\Images\\inbox",
  "model_exists": true,        // false → 隐藏/禁用"开始打标"
  "schema_version": 5,
  "layout_version": 2,
  "device_id": null,
  "images": 19, "collections": 2, "tags": 1788,
  "inbox_pending": 0,          // inbox 里有几个待处理条目
  "index_stale": false         // true → 提示"索引已过期,建议重建"
}}
```

### `search`

```jsonc
{"cmd":"search","args":{"tags_all":["long_hair","solo"],"min_conf":0.3,"limit":50}}
```

```jsonc
{"t":"result","ok":true,"data":{"count":3,"images":[ /* image 对象 */ ]}}
```

- `min_conf` 默认 `0` —— **能查到当初没过阈值的 tag**。想要"只查确定的",传 `0.35`。
- `limit` 为 `null` 表示不限制。

### `image_detail`

```jsonc
{"cmd":"image_detail","args":{"image_id":"20260928-044002_20260928-044002_c772aa6ea37f"}}
```

```jsonc
{"t":"result","ok":true,"data":{
  "image": { /* image 对象 */ },
  "tags": [ {"tag":"1girl","category":0,"confidence":0.9912,"passed":true}, ... ]
}}
```

找不到时 `image` 为 `null`(不是报错)。

### `top_tags`

```jsonc
{"cmd":"top_tags","args":{"limit":50,"min_conf":0.35}}
```

```jsonc
{"t":"result","ok":true,"data":{"tags":[{"tag":"blush","count":19}, ...]}}
```

### `collections`

返回**树序**(父在前,同级按 `side`),不是按随机 coll_id。

```jsonc
{"cmd":"collections"}
{"cmd":"collections","args":{"tag":"halo","min_freq":0.9}}   // 哪些合集大量含 halo
```

```jsonc
{"t":"result","ok":true,"data":{"collections":[ /* collection 对象,已按树排序 */ ]}}
```

前端渲染树用 `depth` 缩进即可 —— **返回顺序就是正确的显示顺序**。

### `collection_tags`

同名合集可能有多个(每次投放算一个新合集),**全部返回**。

```jsonc
{"cmd":"collection_tags","args":{"name":"26.9.28 tg 图集"}}
{"cmd":"collection_tags","args":{"coll_id":"20260928-205303_a174e15fa7d6"}}
```

```jsonc
{"t":"result","ok":true,"data":{"collections":[{
  /* collection 对象 */,
  "tags":[
    {"tag":"breasts","category":0,"count":10,"count_loose":10,
     "freq":1.0,"avg_confidence":0.9431},
    ...
  ]
}]}}
```

- `count` = 过了阈值的图片数,**这就是分子**
- `count_loose` = 只要落库就算,含低分 tag
- `freq` = `count / image_count`,**后端算好,前端直接用**

### `ingest`(流式)

```jsonc
{"cmd":"ingest"}
{"cmd":"ingest","args":{"dry_run":true,"limit":10}}
```

事件顺序(按发生先后):

```jsonc
{"t":"event","event":"model_loading","data":{"path":"..."}}
{"t":"event","event":"model_ready","data":{"seconds":2.4,"provider":"CUDAExecutionProvider"}}
{"t":"event","event":"log","data":{"message":"..."}}              // 杂项
{"t":"event","event":"warning","data":{"kind":"跳过","message":"..."}}
{"t":"event","event":"collection_start","data":{"name":"风景","depth":0,"side":0,
                                                 "images":40,"children":2,"skipped":1}}
{"t":"event","event":"image_done","data":{"name":"a.jpg","outcome":"ok",
                                          "tags":58,"rel_path":"library/...",
                                          "collection":"风景"}}
{"t":"event","event":"progress","data":{"done":53,"total":1234,"rate":12.0,
                                        "eta_seconds":98,"name":"a.jpg"}}
{"t":"event","event":"collection_done","data":{"name":"风景","depth":0,"images":40,
                                               "failed":0,"skipped":1,"structural":false}}
{"t":"event","event":"run_done","data":{"total":1234,"ok":1200,"dupe":30,
                                        "failed":4,"collections":3,"seconds":105,
                                        "avg_ms":87.3}}
{"t":"result","ok":true,"data":{"total":1234, "ok":1200, ..., "exit_code":2,
                                "failures":[{"name":"broken.jpg","reason":"..."}]}}
```

- `outcome` 取值:`ok`(新打标)/ `dupe`(复用已有 tag,`tags` 为 `null`)/ `dry`(试跑)
- `progress` 里 **`eta_seconds` 是后端算好的**,前端直接显示,别自己反推
- `structural: true` 表示这个合集只有子合集、自己没有图片
- `exit_code`:`0` 全成功 / `2` 部分失败

### `delete`

**不可逆。** 图片文件、它的 `_tags.json` 边车、以及它里面嵌的 XMP 会一起消失。
这是唯一自洽的语义 —— library 是真相源,只删库行的话重建索引会把它们复活。

两个数组**至少给一个**,可以混用。合集会连同**整棵子树**一起删(子合集物理
嵌在父目录里,不连带删会留下断链)。

```jsonc
{"cmd":"delete","args":{"image_ids":["20260928-044002_20260928-044002_c772aa6ea37f"]}}
{"cmd":"delete","args":{"coll_ids":["20260928-205303_a174e15fa7d6"]}}
{"cmd":"delete","args":{"image_ids":["..."],"coll_ids":["...","..."]}}
{"cmd":"delete","args":{"coll_ids":["..."],"dry_run":true}}   // 预演,不动任何东西
```

```jsonc
{"t":"result","ok":true,"data":{
  "images": [
    {"image_id":"20260928-044002_..._c772aa6ea37f","rel_path":"library/...",
     "collection_id":1,"status":"deleted"},              // 或 would_delete / partial
    {"image_id":"不存在的id","status":"missing"}
  ],
  "collections": [
    {"coll_id":"20260930-174315_de081c663d7f","name":"子合集","status":"deleted",
     "removed":["a.jpg","index.json","journal_....jsonl"]},
    {"coll_id":"不存在","status":"missing"}
  ],
  "refreshed_collections":[{"coll_id":"...","name":"...","image_count":9}],
  "journal_entries_removed":1,
  "totals":{"images":4,"collections":3,"missing":0,"leftover":0},
  "dry_run":false
}}
```

**六条值得注意的语义:**

1. **id 是磁盘上的名字。** 传数据库数字(`coll_ids:["1"]`)会得到 `missing` ——
   那不是 id。数字换台设备就变了,不可靠。
2. **删合集连整棵子树。** 传一个 `coll_id`,它的所有子合集和全部图片一起删。
3. **幂等。** 重发同一批参数会得到 `missing`,不是报错。中断之后重发一遍即可收尾。
4. **顺序是「先删文件、再删库行」。** 中途崩溃留下「库里有行、盘上没有」——
   一眼看得出的状态,`reindex --prune` 一跑就干净。反过来会留下孤儿文件,
   而重建索引会把它们复活。
5. **`status: "partial"`** 表示文件删不掉(被别的程序占用),但库行已经删了。
   `leftover` 里有具体名字。
6. **一次删除清掉五处:** 图片文件 + 边车、journal 条目、`index.json` 相应内容、
   数据库行、以及(删整个合集时)它的 `index.json` / `journal` 整个文件。

**批量删了再跑 `reindex`,被删的图不会复活** —— 这是删除能成立的前提。

### `reindex`(流式)

```jsonc
{"cmd":"reindex"}
{"cmd":"reindex","args":{"from_images":true}}   // 忽略 journal,最慢但最权威
{"cmd":"reindex","args":{"prune":true,"yes":true}}
```

`prune` 会先报「将删除 N 行」;**不传 `yes` 就不真的删**。

### `verify`(流式)

逐文件巡检,坏的会发 `warning` 事件。

```jsonc
{"t":"result","ok":true,"data":{"checked":19,"bad":0}}
```

### `check`(流式)

```jsonc
{"t":"result","ok":true,"data":{
  "xmp_ok": true, "problems": [],
  "gpu": {"provider":"CUDAExecutionProvider","ms":83.0,"target_size":448,
          "tags_in_csv":10861,"warning":null}
}}
```

XMP 自检**不依赖模型**,所以没装模型也能验证图片读写链路。

---

## 七、取消

**直接 kill 进程就是取消。** 不需要协议层的取消信令 —— 后端的崩溃安全设计
已经处理了这件事:

- 已归档的图片**安全留在 library**
- 未完成的记录是 `pending`,下一轮启动时 `reconcile` 自动收拾
- 暂存区的半成品在下次启动时清空(源文件还在 `inbox`,重跑即可)

前端建议:kill 之后提示「已取消,已处理的 N 张已安全入库」。

---

## 八、给 Rust 侧的一句话总结

> 构造 `{"cmd": ..., "args": {...}}` 写进 stdin,然后循环读 stdout 的行;
> `t=="event"` 就转发给前端,`t=="result"` 就返回并停止。stderr 和 stdout
> 都要在独立线程里读。

**没有别的约定** —— 请求、响应、参数表、image / collection / tag 对象,
所有命令都是同一套形状。
