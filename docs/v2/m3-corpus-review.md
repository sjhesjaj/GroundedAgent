# M3 Phase 1 正式语料审阅清单

状态：**已审阅，已冻结**。35 篇均已由用户审阅，自拟口径用户已确认；冻结在本 PR 合入 main 后生效，之后修改语料需要用户同意。

共 35 篇：12 篇规则复述，23 篇知识库独有文档。其中在期知识库独有内容为运费 5、退款 5、凭证 2、FAQ 3、赠品 2、价格保护 2、发票 2；另有运费和退款过期存档各 1 篇。前 27 篇主要依据六条现有规则及 Phase 0 的运费、退款 FAQ；新增赠品、价格保护、发票及过期存档共 8 篇经用户授权按虚构演示店铺服务口径起草，并标注“演示口径（自拟，用户已确认）”。其中四篇现行退款文档补充了指向赠品扣减口径的提示，来源栏亦标明自拟部分。这些口径不代表真实商家的政策。

规则复述仍只引用单一 `restates`，窗口日数严格沿用原规则的 7 / 15 / 30 个自然日。演示业务时间为 **2026-11-15 10:00 +08:00**；双十一活动从 2026-11-01 生效、至 2026-12-01 零时失效，旧版两篇在该业务时间已过期。运费、退款现行口径保留 Phase 0 已有数字；旧版自拟口径刻意采用不同数字，用于之后检查过期内容过滤。

下表“类型”把内容角色与现有 `doc_type` 同时列出；“来源”中的“原 Phase 0”指合并后 main `74ab12b` 的原始文档。正文、近似重复和品类附注均已审阅。本轮仅删除元说明，不改事实、数字、日期、条件、品类前提或 front matter；尚未作检索调参。

| doc_id | 标题 | 类型 | restates | 陷阱 | 事实来源 |
|---|---|---|---|---|---|
| kb-standard-return | 标准退货申请时间说明 | 规则复述（guide） | standard-return | 标准/活动近词组 A；特殊品类附注 | policy_sources/standard-return.md |
| kb-standard-return-counting | 标准退货时间从哪天开始算 | 规则复述（faq） | standard-return | 标准/活动近词组 B；与 kb-standard-return 近似表述 | policy_sources/standard-return.md |
| kb-standard-exchange | 标准换货申请时间说明 | 规则复述（guide） | standard-exchange | 退货/换货近词；服装专门规则附注 | policy_sources/standard-exchange.md |
| kb-standard-exchange-counting | 标准换货时间与库存核对问答 | 规则复述（faq） | standard-exchange | 退货/换货近词；与标准换货说明近似重复 | policy_sources/standard-exchange.md；policy_sources/apparel-exchange.md（仅专门规则存在） |
| kb-apparel-exchange | 服装换货时间与吊牌说明 | 规则复述（guide） | apparel-exchange | 品类附注 A；与标准换货措辞相近 | policy_sources/apparel-exchange.md |
| kb-apparel-exchange-category-note | 服装品类换货附注问答 | 规则复述（faq） | apparel-exchange | 品类附注 A；服装/标准近词；近似重复 | policy_sources/apparel-exchange.md |
| kb-custom-non-returnable | 定制商品无理由退货限制说明 | 规则复述（guide） | custom-non-returnable | 品类附注 B；限制与质量处理路径区分 | policy_sources/custom-non-returnable.md |
| kb-custom-category-note | 定制品类限制与质量争议问答 | 规则复述（faq） | custom-non-returnable | 品类附注 B；与定制限制说明近似重复 | policy_sources/custom-non-returnable.md |
| kb-quality-handoff | 质量争议人工核实说明 | 规则复述（guide） | quality-handoff | 处理路径与完成状态区分 | policy_sources/quality-handoff.md |
| kb-quality-evidence | 质量争议材料与人工处理问答 | 规则复述（faq） | quality-handoff | 与质量争议说明近似重复；路径/状态近词 | policy_sources/quality-handoff.md |
| kb-november-promo | 双十一活动售后说明 | 规则复述（promotion） | november-promo-return | 标准/活动近词组 A；活动有效期；特殊品类附注 | policy_sources/november-promo-return.md |
| kb-november-promo-counting | 双十一退货时间从哪天开始算 | 规则复述（promotion） | november-promo-return | 标准/活动近词组 B；活动有效期；近似重复 | policy_sources/november-promo-return.md |
| kb-return-shipping | 退货运费说明 | 知识库独有（faq） | — | 运费总览/凭证说明近似重复组 A | 原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-shipping-quality-cost | 质量问题与错发商品寄回运费 | 知识库独有（guide） | — | 与运费总览近似表述 | 原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-shipping-nonquality-cost | 不喜欢或尺码不合适的寄回运费 | 知识库独有（faq） | — | 非质量运费/运费险相近措辞 | 原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-shipping-insurance | 运费险到账与商城退款的区别 | 知识库独有（faq） | — | 72 小时/退款工作日相近时间词；近似重复 | 原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-shipping-return-parcel | 退货包裹寄回与运费凭证说明 | 知识库独有（guide） | — | 运费总览/凭证说明近似重复组 A | 原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-refund-timing | 退款到账时间 | 知识库独有（faq） | — | 到账总览/渠道说明近似重复组 B | 原 Phase 0 knowledge_base/kb-refund-timing.md；赠品扣减提示为演示口径（自拟，用户已确认） |
| kb-refund-channels | 微信支付宝银行卡退款到账说明 | 知识库独有（guide） | — | 到账总览/渠道说明近似重复组 B | 原 Phase 0 knowledge_base/kb-refund-timing.md |
| kb-refund-initiation | 仓库验收后何时发起退款 | 知识库独有（faq） | — | 签收/验收近词；发起/到账近词 | 原 Phase 0 knowledge_base/kb-refund-timing.md |
| kb-refund-coupon | 实付金额与优惠券退回说明 | 知识库独有（guide） | — | 退款款项/优惠券近词 | 原 Phase 0 knowledge_base/kb-refund-timing.md；赠品扣减提示为演示口径（自拟，用户已确认） |
| kb-refund-working-days | 退款工作日与不同到账起点 | 知识库独有（faq） | — | 自然日/工作日/小时近词；近似重复 | 原 Phase 0 knowledge_base/kb-refund-timing.md；原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-evidence-shipping | 运费凭证与包裹订单号纸条 | 知识库独有（guide） | — | 运费凭证/订单纸条近词；近似重复 | 原 Phase 0 knowledge_base/kb-return-shipping.md |
| kb-evidence-purpose | 售后材料分别说明什么 | 知识库独有（faq） | — | 商品凭证/运费凭证近词；材料用途对比 | 原 Phase 0 knowledge_base/kb-return-shipping.md；policy_sources/quality-handoff.md（仅材料表述） |
| kb-faq-shipping-refund | 寄回运费与商品退款常见问答 | 知识库独有（faq） | — | 运费返还/商品退款/保险理赔近词 | 原 Phase 0 knowledge_base/kb-return-shipping.md；原 Phase 0 knowledge_base/kb-refund-timing.md；赠品扣减提示为演示口径（自拟，用户已确认） |
| kb-faq-parcel-refund | 寄出签收验收与退款常见问答 | 知识库独有（faq） | — | 寄出/签收/验收近词；近似重复 | 原 Phase 0 knowledge_base/kb-return-shipping.md；原 Phase 0 knowledge_base/kb-refund-timing.md |
| kb-faq-coupon-channel | 优惠券与退款渠道常见问答 | 知识库独有（faq） | — | 券退回/原路款项退回近词；近似重复 | 原 Phase 0 knowledge_base/kb-refund-timing.md；赠品扣减提示为演示口径（自拟，用户已确认） |
| kb-gift-return-packaging | 赠品随商品寄回与装箱核对 | 知识库独有（guide） | — | 与赠品金额问答近似重复；赠品/另一商品混放 | 演示口径（自拟，用户已确认） |
| kb-gift-refund-value | 赠品未随寄时的退款金额问答 | 知识库独有（faq） | — | 与赠品装箱指南近似重复；120−10＝110 仅示例 | 演示口径（自拟，用户已确认） |
| kb-price-protection-guide | 价格保护周期与差额核对指南 | 知识库独有（guide） | — | 与价保问答近似重复；7 个自然日从付款次日起算 | 演示口径（自拟，用户已确认） |
| kb-price-protection-faq | 降价了如何核对价格保护差额 | 知识库独有（faq） | — | 与价保指南近似重复；7 天价保不可混为退换时限 | 演示口径（自拟，用户已确认） |
| kb-invoice-request-guide | 电子普通发票信息准备指南 | 知识库独有（guide） | — | 邮箱/抬头信息混淆 | 演示口径（自拟，用户已确认） |
| kb-invoice-correction-faq | 电子发票抬头写错与文件重发问答 | 知识库独有（faq） | — | 重发/重开混淆；最终结算金额/原标价 | 演示口径（自拟，用户已确认） |
| kb-return-shipping-archived | 退货运费说明：2025 年存档旧版 | 知识库独有（faq） | — | 过期旧版；10 元/96 小时与在期版 20 元/72 小时不同 | 演示口径（自拟，用户已确认） |
| kb-refund-timing-archived | 退款到账时间：2025 年存档旧版 | 知识库独有（faq） | — | 过期旧版；3 工作日发起、支付宝 3–5/银行卡 5–10 与在期版不同 | 演示口径（自拟，用户已确认） |

## 已审阅的陷阱覆盖

| 陷阱类别 | 至少两处具体位置 | 审阅重点 |
|---|---|---|
| 已过期旧版本 | kb-return-shipping-archived；kb-refund-timing-archived | effective_to 已过；旧版数字只用于存档，不能混入在期答复 |
| 标准与活动措辞相近 | kb-standard-return 与 kb-november-promo；kb-standard-return-counting 与 kb-november-promo-counting | 相近的“签收次日／自然日”措辞对应不同窗口及有效期 |
| 近似重复 | kb-return-shipping 与 kb-shipping-return-parcel；kb-refund-timing 与 kb-refund-channels | 内容相似但各自强调运费材料、支付渠道；不能从相似标题补出办理状态 |
| 品类附注 | kb-apparel-exchange 与 kb-apparel-exchange-category-note；kb-custom-non-returnable 与 kb-custom-category-note | 服装吊牌及再次销售前提；定制无理由限制不替代质量争议处理 |

用户已确认以下内容：

1. `kb-gift-return-packaging`、`kb-gift-refund-value`：赠品寄回及未随寄时金额处理，均为自拟口径。
2. `kb-price-protection-guide`、`kb-price-protection-faq`：自拟价保周期和差额处理，付款起点及服务事项应与退换货窗口区分。
3. `kb-invoice-request-guide`、`kb-invoice-correction-faq`：自拟电子发票信息、重发及重开流程。
4. 两篇 `*-archived`：有意写不同数字的过期口径，确认存档陷阱足够清晰且不会被当成现行承诺。
5. `kb-standard-return`、`kb-november-promo`、`kb-custom-category-note`：确认标准／活动／特殊品类的复述没有改写资格含义。

自拟内容只参考通行主题和处理方式；参数为本演示商城设定，不宣称与真实平台完全一致。主题参考：[京东赠品说明](https://help.jd.com/user/issue/327-988.html)、[京东价保说明](https://help.jd.com/user/issue/291-4537.html)、[京东发票修改](https://help.jd.com/user/issue/502-660.html)。

## 删除元说明与短文例外

2026-10-09 仅删除元说明片段，共 35 篇、151 处；正文事实、数字、活动日期、条件、品类前提和 front matter 保持不变。两篇旧版保留标题中的“2025 年存档旧版”，正文过期自声明删去，失效时间由 effective_to 体现。

正文按去标题、去空白并计入数字与标点统计，为 **59–316 字**。普通文档要求 150–800 字；以下 13 篇不足 150，用户明确决定“这些篇允许短于 150 字，保留现有事实直接冻结”。例外在 knowledge_base/reviewed-short-bodies.json 中绑定全文经 LF 规范化后的 SHA256，内容改变即失效。

| doc_id | 正文字数 | 决定 |
|---|---:|---|
| kb-apparel-exchange | 138 | 用户已确认短文例外；不补字 |
| kb-custom-category-note | 88 | 用户已确认短文例外；不补字 |
| kb-custom-non-returnable | 59 | 用户已确认短文例外；不补字 |
| kb-evidence-purpose | 117 | 用户已确认短文例外；不补字 |
| kb-quality-evidence | 80 | 用户已确认短文例外；不补字 |
| kb-quality-handoff | 62 | 用户已确认短文例外；不补字 |
| kb-refund-channels | 134 | 用户已确认短文例外；不补字 |
| kb-refund-coupon | 140 | 用户已确认短文例外；不补字 |
| kb-refund-initiation | 132 | 用户已确认短文例外；不补字 |
| kb-shipping-insurance | 126 | 用户已确认短文例外；不补字 |
| kb-shipping-quality-cost | 129 | 用户已确认短文例外；不补字 |
| kb-standard-exchange-counting | 125 | 用户已确认短文例外；不补字 |
| kb-standard-exchange | 114 | 用户已确认短文例外；不补字 |

## 检查记录

正式在线建缓存和严格只读离线复建均通过：**35 篇 / 95 段**，资格 lint、复述数字 / 单位和生效期检查通过；12 篇规则复述、23 篇知识库独有。业务时间仍为 2026-11-15T10:00:00+08:00，33 篇在期、2 篇存档已过期。真实 bge-m3 段落向量为 1024 维，缓存不随 Git 分发。

全量离线 unittest 的最终数量、隔离结果和缓存不变记录见 [m3-phase1-offline-results.json](m3-phase1-offline-results.json)。前端 API 20/20 通过。冻结的 Stage 6 目录、规则和 golden fixture 均未修改；当前默认策略仍为 stage6。

逐篇字数、原始文件及规范化 hash 见 [构建记录](m3-corpus-build.json)，命令、验证与冻结边界见 [交付报告](m3-phase1-report.md)。本轮未编写评测集、检索调参或实现 get_my_pending_requests；Phase 2 从合并后的 main 开始。
