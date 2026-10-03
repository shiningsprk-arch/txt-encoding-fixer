# TXT编码修复（MyBooks Toolbox 插件）

> 工具 ID：`txt_encoding_fixer`　作者：黏菌　版本：0.1.1
> 书库 → 工具箱 → TXT编码修复 → 选择书籍 → 分析编码 → 执行修复

## 功能

检测书籍 **TXT** 格式的文本编码，修复乱码后**另存为新书**（原书文件零改动）：

1. **编码检测**（BOM 优先 → 候选编码严格解码打分 → chardet 三段采样投票 → mojibake 反转链 → 可读性评分；另含无 BOM UTF-16 车道结构校验、日韩编码脚本一致性识别、西文 latin-1 误读预检、有损兜底恢复与不可逆拒修）
2. **修复**：解码为正确的 UTF-8（无 BOM）写出；带损文件（含少量无法还原的替换符）在检测报告与完成消息中明确警示，损伤超门槛（≥20 处且 >1%）直接拒绝修复
3. **新书入库**：复用原书完整元数据（作者 / 标签 / 出版社 / 丛书 / 简介 / 语言 / 封面），标题追加「（编码修复版）」

典型场景：繁体 BIG5 小说被程序按 GBK 误读后以 UTF-8 存盘（表现为「锟斤拷」乱码）——检测器能自动反转恢复。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/toolbox/txt_encoding_fixer/analyze` | 同步检测 `{book_id}` → 编码 / 置信度 / 乱码标记 / 检测依据 / 修复后 500 字预览 |
| POST | `/api/toolbox/txt_encoding_fixer/fix` | 后台执行 `{book_id}`：解码修复 → UTF-8 无 BOM 写出 → 新书入库 |
| GET | `/api/toolbox/txt_encoding_fixer/progress` | 轮询 `status / progress / stage` |

## 文件清单

```
TXT编码修复/
├── README.md
├── webserver/
│   ├── handlers/toolbox.py            # 修改版：+3 handler（analyze/fix/progress）+3 路由
│   └── toolbox/
│       ├── toolset.py                 # 修改版：+2 import +2 register（含 text_replace，见下）
│       ├── txt_encoding_fixer.py      # 插件主体（Tool 类）
│       └── utils/
│           ├── encoding_detect.py     # 公共模块①：编码检测（与 text_replace 共享）
│           └── book_utils.py          # 公共模块②：get_book_file / import_as_new_book
├── app/
│   ├── src/pages/toolbox/txt_encoding_fixer.vue   # Vue 2.6 + Vuetify 2 页面
│   └── locales/{en,zh,zh-TW}.json     # 修改版：+txtEncodingFixer 块（另含 textReplace 块，见下）
└── tests/test_encoding_detect.py      # standalone 单测（65 个）
```

## 安装部署（4 处修改）

将以下文件复制到 mybooks 源码对应位置（与主线 v4.4.1 布局一致）：

| 源文件 | 目标位置 |
|--------|----------|
| `webserver/toolbox/txt_encoding_fixer.py` | `webserver/toolbox/` |
| `webserver/toolbox/utils/encoding_detect.py` | `webserver/toolbox/utils/` |
| `webserver/toolbox/utils/book_utils.py` | `webserver/toolbox/utils/` |
| `webserver/toolbox/toolset.py` | **覆盖** `webserver/toolbox/toolset.py` |
| `webserver/handlers/toolbox.py` | **覆盖** `webserver/handlers/toolbox.py` |
| `app/src/pages/toolbox/txt_encoding_fixer.vue` | `app/src/pages/toolbox/` |
| `app/locales/en.json` / `zh.json` / `zh-TW.json` | **覆盖** `app/locales/` 同名文件 |

### 与「正文查找替换」插件的关系

两个插件共享 `encoding_detect.py` / `book_utils.py`，且本文件夹的修改版
`toolset.py` 与 `handlers/toolbox.py` **已同时包含两个插件的注册与路由**
（6 handler + 6 路由），locales 亦同时含 `txtEncodingFixer` + `textReplace` 两块：

- 只装本插件：直接按上表复制即可（多余的另一插件注册行无害——对应
  `text_replace.py` 不存在时导入会报错，见下方说明）；
- 同时装两个：将 `正文查找替换` 文件夹中的 `text_replace.py` 一并复制即可，
  **两个文件夹的修改版文件内容一致，任取其一**，无需手工合并。

> 注意：本文件夹 `toolset.py` / `handlers/toolbox.py` 会 import
> `TextReplaceTool`。若**只装本插件**，请删除修改版中的这两处引用
> （`toolset.py` 的 import + register 各 1 行；`toolbox.py` 的 import 1 行 +
> `AdminTextReplace*` 3 个 handler + 3 条路由），或直接改用
> `正文查找替换` 文件夹中同名的修改版（内容一致，反向删 `TxtEncodingFixerTool` 引用）。

### 依赖

- `chardet`（`requirements.txt` 已包含，v7.x）：编码检测投票，缺失时自动退化为纯规则检测。

## 运行测试

```bash
python -m unittest discover -s tests -v
# 或：python tests/test_encoding_detect.py
```

覆盖：UTF-8 / GB18030 / BIG5 / UTF-16 无 BOM / 各类 BOM 及 BOM 错配 / 英文 / 二进制垃圾 /
空文件 / 解码往返 / 乱码反转恢复（BIG5-as-GBK、UTF8-as-GBK、ANSI 单双层、西文误读）/
幂等性 / 采样边界 / 有损兜底 / 不可逆拒修 / str 输入防御。

## 测试库实测步骤

1. 准备一本 **GB18030 编码** 的 TXT 书入库（或用脚本造乱码样本：
   `open('x.txt','wb').write(繁体文本.encode('big5').decode('gb18030').encode('utf-8'))`）；
2. 书库 → 工具箱 → TXT编码修复 → 搜索并选择该书；
3. 点「分析编码」：应显示 `big5`、置信度 100%、乱码已恢复、预览为正确繁体；
4. 点「执行修复」：右上角消息通知「修复成功」，书库出现标题带「（编码修复版）」的新书；
5. 阅读新书确认正文正确、封面与作者与原书一致；原书文件未被改动。
