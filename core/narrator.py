"""叙事素材池：顾客点了什么、为什么给差评、商家怎么出餐慢。

判定依旧由本地概率决定（保证公平、经济曲线不崩），
这个模块只负责把「为什么」编得好看，做到每单不重样。

素材由 LLM 批量生成后落盘缓存，取用时零延迟；
模型不可用 / 池子为空时回退到内置文案，**绝不会因此卡住结算**。

纯 stdlib，不 import astrbot，方便 dry_run / simulate 直接跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time

# 池子种类。每一条都要"能对上账"：
#   shops         品类|店名|商品  —— 一条就凑齐了「哪家店、卖的是啥、顾客要的是啥」
#   note          顾客备注（奇怪要求）
#   merchant_slow 出餐慢的具体原因（必须能解释为什么等久了）
#   merchant_fast 出餐顺利（顺）+ late 路上拖慢（逆）→ 直接决定超没超时
#   bad_late/bad_pack/bad_damage/bad_karen  差评的来源，不能混着用
KINDS = ("shops", "names", "note", "note_goods", "bulk", "praise",
         "bad_late", "bad_pack", "bad_damage", "bad_karen",
         "merchant_slow", "merchant_fast", "late", "smooth", "grumpy",
         "fail", "fail_cust")

SHOP_SEP = "|"

# 池子低于这个数就补货；一次补多少；两次补货之间的最短间隔（秒）
MIN_POOL = 6
REFILL = 24
COOLDOWN = 600
# 失败别等满 COOLDOWN：模型偶尔抽风/超时，90 秒后应该允许再试一次
RETRY_AFTER = 90
MAX_POOL = 240
# 被取用这么多次之后就当「用熟了」，重新向模型要一批（店名/称谓不能永远那 24 个）
ROTATE_AFTER = 60

# 每种池子给模型的说明（"没有限制"：不限定品类、不限定风格）
SPEC = {
    "shops": (
        "一条外卖/同城跑腿订单的三段信息，用竖线分隔：`品类|店名|顾客买的东西`。"
        "品类从这些里挑：餐饮、药房、超市、生鲜、水果、花店、宠物、数码、"
        "五金、建材、蛋糕、奶茶、文具、服装、洗衣、跑腿。"
        "店名要照着现实里真有的店来写（华莱士、蜜雪冰城、益丰大药房、"
        "大润发、盒马鲜生、百果园、花点时间、好利来、喜茶、晨光文具、"
        "顺丰同城这类，连锁或本地小店都行），别带引号。"
        "**商品必须这家店真的会卖**：肯德基只能卖炸鸡汉堡，不能卖水果；"
        "益丰大药房只能卖药和保健品；花店只能卖花。商品要具体到份量或要求。"
        "十六个品类都要出现，别重复。示例：`药房|益丰大药房|一盒布洛芬`"
    ),
    "names": (
        "外卖订单收件人的称呼，要像真实中国人：姓 + 先生/女士/阿姨/大叔/"
        "师傅/小姐姐/同学/老师/大爷。姓要常见（王李张刘陈杨黄赵吴周徐孙马朱胡"
        "郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈），"
        "先生/女士占多数，偶尔来一个「王师傅」「刘阿姨」这种更接地气的。四个字以内"
    ),
    "note": (
        "外卖订单上顾客写的备注，越离谱越好笑但真实：菜里别放葱、"
        "别敲门家里有狗、送到楼下喊一声就行、必须敲门敲三下、"
        "顺路帮我带包烟（不给钱）、汤别洒一滴、要一次性筷子两根、"
        "放门口鞋柜上、让店家多给两包辣油。语气是顾客写的原话"
    ),
    "note_goods": (
        "**非餐饮类**订单（药、花、快递、数码、衣服、五金、菜）上顾客写的备注，"
        "不能提吃和汤：放前台就行、别按门铃我妈在睡、到楼下打电话我下去拿、"
        "轻拿轻放别磕、送到小区门口等我两分钟、放快递柜我自己取、"
        "先别拆包装我要送人、能不能放物业、我在电梯口等、"
        "让骑手顺手把旧箱子带走、货到付款记得带零钱。语气是顾客写的原话"
    ),
    "bulk": (
        "外卖里的大件单：要占满一个车斗、得捆着送的货。整箱的、成打的、"
        "易碎或沉的，一句话说清是什么（整箱矿泉水、一箱啤酒、煤气罐、"
        "两袋猫砂、折叠椅、打印纸一箱、桶装水、菜市场代买一筐菜）"
    ),
    "praise": "顾客给骑手好评的理由，一句话，要具体（饭还热着、汤没洒、比预计早到）",
    "bad_late": "顾客因为「送晚了」给差评的理由，一句话，重点是等太久/迟到（迟到半小时、等到汤都凉了）",
    "bad_pack": "顾客因为「包装/温度/洒漏」给差评的理由，一句话，重点是餐坏了（汤洒了、压扁了、串味）",
    "bad_damage": (
        "不是餐的单子（药、花、快递、数码、衣服、水果、建材）送坏了的差评理由，"
        "一句话，重点是包装压坏/磕碰/淋湿/花蔫了；"
        "**别说清里面是什么**（写成包装、盒子、袋子、箱角），"
        "因为同一句会配给药、花、快递各种单；也不要提汤、饭、菜、筷子"
    ),
    "bad_karen": (
        "无理取闹的差评：顾客给差评的理由完全站不住脚，越离谱越好笑"
        "（因为香菜没切碎、因为小哥没笑、因为敲门声音太大、"
        "因为外卖员长得不像明星、因为袋子颜色不喜欢）"
    ),
    "merchant_slow": (
        "餐厅出餐慢的具体原因，一句话，要能解释「为什么等了很久」"
        "（后厨排队二十多分钟、系统把单排到最后、单子打错重做）"
    ),
    "merchant_fast": "餐厅出餐很顺的情形，一句话（提前打包好、店员一喊就拿到、拎了就走）",
    "late": "骑手路上遇到的具体阻碍，一句话，必须是会真的拖慢时间的事（封路、电梯坏了、找不到楼、堵死）",
    "smooth": "骑手路上特别顺的情形，一句话（一路绿灯、保安直接放行、抄了条近道）",
    "grumpy": (
        "顾客等太久时的抱怨原话（还没给差评，只是在电话里/群里发牢骚），"
        "一句话，语气急但不脏（还要多久、我饿得胃疼、都吃完泡面了）"
    ),
    "fail": (
        "配送失败的具体原因，一句话，要真能导致「没送到」（电话打不通、"
        "门禁进不去、地址是隔壁小区、人不在家、餐洒得没法交、被保安拦下），"
        "写画面别写结论"
    ),
    "fail_cust": (
        "顾客没收到货、单子被退回商家之后说的话，一句话。"
        "要像真人在生气或放弃（「算了，我退单了」「那我下楼随便吃点吧」"
        "「等了你一个小时，人都凉了」），**不能是还在等**的口气"
        "（不许写「快点」「还有多久」这种催单话，因为他已经知道送不到了）"
    ),
}

_BAD_WORDS = (
    "习近平", "共产党", "政治", "台独", "法轮功", "六四", "新疆独立",
    "操你", "草你", "傻逼", "婊子", "强奸", "自杀",
)

# 没有模型时也能玩的本地池
FALLBACK = {
    # 品类|店名|商品：店名照现实里的店写，卖的必须是这家店真会卖的东西
    "shops": [
        "餐饮|华莱士|两份炸鸡汉堡套餐", "餐饮|沙县小吃|拌面扁肉套餐",
        "餐饮|杨国福麻辣烫|一碗麻辣烫，加宽粉和鱼豆腐", "餐饮|老乡鸡|一份老母鸡汤饭",
        "餐饮|肯德基|一个全家桶，多要两包番茄酱", "餐饮|张亮麻辣烫|大份微辣，不要香菜",
        "餐饮|绝味鸭脖|半斤鸭脖，微辣", "餐饮|南城香|一份红烧肉饭，多加米饭",
        "餐饮|阿香米线|一碗番茄肥牛米线，多加汤", "餐饮|阿婆馄饨|两碗馄饨，一碗不要葱",
        "餐饮|重庆小面|小面二两，多辣", "餐饮|南门烧烤|二十串羊肉串，多孜然",
        "药房|益丰大药房|一盒布洛芬，孩子发烧急用", "药房|老百姓大药房|两包创可贴和一瓶藿香正气",
        "药房|叮当快药|退烧药一盒，半小时内要", "药房|同仁堂|一罐蜂蜜和两包枸杞",
        "药房|大参林|降压药一盒，别拿错牌子", "药店|海王星辰|一瓶碘伏和一把棉签",
        "超市|大润发|两袋 20 斤的大米", "超市|永辉超市|洗衣液一大桶和一包袜子",
        "超市|罗森便利店|两桶泡面加一根火腿肠", "超市|全家便利店|一箱纯净水 12 瓶",
        "超市|便利蜂|盐、醋和一包饺子皮", "超市|物美超市|一箱抽纸和两包湿巾",
        "生鲜|盒马鲜生|一斤排骨，要现切的", "生鲜|钱大妈|两条鲫鱼，杀好去鳞",
        "生鲜|叮咚买菜|一把韭菜和半斤肉馅", "生鲜|美团买菜|两块嫩豆腐和一把小葱",
        "水果|百果园|一箱沙糖桔，挑甜的", "水果|鲜丰水果|两斤樱桃，要硬的",
        "水果|街角水果摊|半个西瓜，去皮切块", "花店|花点时间|一束白玫瑰，配满天星",
        "花店|野兽派|生日花束，上午十点前送到", "花店|四季花坊|一盆薄荷，好养的那种",
        "宠物|宠物之家|一袋猫砂和一罐金枪鱼罐头", "宠物|佩蒂宠物|狗粮一袋 5 公斤",
        "宠物|爱心宠物医院|猫用驱虫药一盒", "数码|小米之家|一根数据线和一个充电宝",
        "数码|顺电|一个 65 瓦快充头", "数码|华强北数码铺|换个手机屏幕，等着用",
        "五金|老周五金店|一盒螺丝和一管玻璃胶", "五金|建材市场 3 号摊|两米水管，切好",
        "建材|居然之家|一桶乳胶漆，白色", "建材|大华建材|一袋水泥，50 斤",
        "蛋糕|好利来|六寸生日蛋糕，写上生日快乐", "蛋糕|幸福西饼|十二个蛋挞，要现烤的",
        "蛋糕|面包新语|一盒肉松小贝", "奶茶|蜜雪冰城|四杯柠檬水，去冰",
        "奶茶|喜茶|两杯芝士葡萄，少糖", "奶茶|茶百道|三杯杨枝甘露，去冰",
        "奶茶|奈雪的茶|两杯霸气橙子，少冰", "奶茶|茶颜悦色|三杯幽兰拿铁，去冰",
        "文具|晨光文具|一盒中性笔和两本笔记本", "文具|得力文具|打印装订一份 20 页材料",
        "服装|优衣库|一件白 T 恤，M 码", "服装|海澜之家|一件免烫衬衫",
        "洗衣|福奈特|两件衬衫，洗好取回", "洗衣|衣本色干洗|一件西装外套，要熨平",
        "跑腿|顺丰同城|帮我把文件送到城南小区 3 栋", "跑腿|闪送|把钥匙送到金奥大厦前台",
        "跑腿|美团跑腿|去银行代排队取号", "生鲜|老王菜摊|一把韭菜和半斤肉馅",
    ],
    "names": [
        "王先生", "刘女士", "李阿姨", "张大爷", "陈老师", "杨同学",
        "黄师傅", "赵女士", "吴先生", "周小姐", "徐大姐", "孙先生",
        "马女士", "朱师傅", "胡阿姨", "郭先生", "何女士", "高同学",
        "林小姐", "罗大爷", "郑女士", "梁先生", "谢阿姨", "宋先生",
        "唐女士", "许师傅", "韩先生", "冯女士", "邓同学", "曹先生",
        "彭女士", "曾阿姨", "田师傅", "董先生", "袁女士", "潘大爷",
        "于先生", "蒋女士", "蔡师傅", "余先生", "杜女士", "叶同学",
        "程先生", "苏女士", "魏师傅", "吕先生", "丁女士", "任大爷",
        "沈阿姨", "姚先生", "卢女士", "姜师傅", "崔先生", "钟女士",
        "谭先生", "陆女士", "汪先生", "范女士", "金先生", "石女士",
    ],
    "bulk": [
        "整箱矿泉水 24 瓶，搬上 6 楼", "一箱啤酒 24 听，别晃", "煤气罐一罐，急用",
        "两袋猫砂 20 斤，腰不好", "桶装水两桶，要扛", "打印纸一箱，走货梯",
        "菜市场代买一筐菜，带泥的", "折叠桌一张，1.2 米", "整箱牛奶 12 盒，玻璃瓶",
        "搬家纸箱三个，装书", "一箱苹果，磕不得", "火锅食材整箱，需冷藏",
        "儿童滑板车，包装很大", "两箱抽纸，体积大", "电动车电池一块，沉",
        "整箱方便面，24 包", "健身哑铃一对，共 20 公斤", "花盆两盆，土别撒",
        "一箱冻品，越快越好", "婚纱照大相框，易碎", "鱼缸一个，要平放",
        "整箱饮料 30 瓶，压车斗",
    ],
    "praise": [
        "小哥跑得挺快，饭还热着", "包装没洒，辛苦了", "比预计早到十分钟，稳",
        "雨天还这么准时，谢谢", "备注的要求都记得，好评", "汤一点没洒，专业",
        "态度挺好，下次还点你家", "送到楼下还顺手带了垃圾，好人",
        "这么快就到了？佩服", "骑手没催我，舒服", "份量足，送得也快",
        "第一次见这么准时，五星", "楼下就闻到香味了", "包装封得严，放心",
        "辛苦了，天这么冷", "小哥笑得挺开心", "一丝不苟，谢谢",
        "速度点赞，饭还是烫的", "备注写对了，心细", "这单送得漂亮",
    ],
    "note_goods": [
        "放前台就行，别上楼", "别按门铃，我妈在睡觉", "到楼下打个电话，我下去拿",
        "轻拿轻放，别磕着", "送到小区门口，等我两分钟", "放快递柜，我自己取",
        "先别拆包装，我要送人", "放物业吧，谢谢", "我在电梯口等你",
        "顺路把旧箱子带走", "货到付款，记得带零钱", "别放地上，潮",
        "送到三号门，别走错", "放门口架子上，别敲门", "我手机静音，多打两次",
        "包装朝上放，别倒过来", "给单位前台签收，别给我打电话", "东西有点重，麻烦搬上来",
        "放车库门口就行", "我一会儿就下来，别走",
    ],
    "note": [
        "菜里别放葱，有葱就退货", "放门口，别敲门，家里有狗",
        "必须敲门三下，别按门铃", "汤别洒一滴，洒了我不要",
        "顺路帮我带包烟，回来给你钱（其实不给）", "多给两包辣油，不然差评",
        "米饭要热的，凉了我不吃", "到楼下喊一声，我就下来",
        "放鞋柜上，别放地上有蚂蚁", "我腿脚不方便，麻烦送上楼",
        "别打我电话，我妈在睡觉", "孩子刚睡，别敲门",
        "一次性筷子要两根，少一根就不吃了", "可乐要冰的，常温的别送",
        "香菜单独装，一根都别混进去", "汤和面分开，混一起算你输",
        "请戴手套拿餐盒，我有点洁癖", "上楼别踩我家门口那块垫子",
        "6 楼没电梯，辛苦了，有小费", "袋子系紧，别让汤流出来",
        "不要打电话，微信消息叫我", "帮我看门口有没有快递，顺手带上来",
        "餐具要三份，家里来人了", "到楼下发个消息，别按门铃",
    ],
    "bad_late": [
        "迟了四十分钟，汤都凝了", "等到孩子都睡了才送来",
        "说十分钟到，结果等了一小时", "打电话不接，我在楼下等了二十分钟",
        "超时半小时，一句道歉都没有", "饭点送成这样，饿过头了",
        "到的时候菜全凉了，米饭硬了", "提前点送达，人根本没到",
        "我等得都下楼吃了碗面", "说堵车，可我看着他在别处转",
        "汤洒了还迟到，双重打击", "催了三次才回消息",
        "送到已经过了饭点", "晚到还怪我地址写错",
    ],
    "bad_pack": [
        "汤洒了半袋，袋子全是油", "餐盒压扁了，菜都挤一起",
        "奶茶倒了一半，吸管也断", "汤和面混一起，糊成一坨",
        "外卖箱串味，饭有股油烟味", "塑料袋破了，菜掉在楼道",
        "少了一份小菜，问就说不知道", "筷子没给，手抓着吃",
        "少给一包酱，味道全毁了", "备注不要辣，还是辣",
        "饭凉透了，没法吃", "盒盖没扣好，汤全流袋子里",
    ],
    "bad_damage": [
        "包装压扁了一个角，里面硌坏了", "盒子被踩了一脚，皱得不成样",
        "外包装全皱了，像从垃圾堆捡的", "包装被雨淋软了，一提就破",
        "袋子里东西滚得乱七八糟", "包装上破了个洞，能看见里面",
        "封口开了，东西少了一件", "被压在最底下，整个变形了",
        "盒角都塌了，硬塞进去的", "提手断了，东西撒了一地",
        "雨太大，纸箱子湿透发软", "堆在一起压了一路，全变形",
    ],
    "bad_karen": [
        "香菜没切碎，咽不下去", "外卖员没笑，态度不好",
        "敲门声太大，吓到我家猫", "骑手长得不像明星",
        "袋子是蓝色，我不喜欢", "没主动帮我扔垃圾",
        "长得太帅，我老公不高兴", "晚了 1 分钟，就 1 分钟",
        "汤太烫，烫到我嘴", "我说了不要香菜，他居然说没有",
        "外卖员走路声音太大", "我没在家，他也送到了",
        "他没夸我家狗可爱", "饭太好吃，吃完想再点一份，气死我了",
    ],
    "merchant_slow": [
        "后厨排着长队，等了二十多分钟", "系统把我的单排到了最后",
        "单子打错了，重做又等十分钟", "打包的人手不够，袋子堆一地",
        "骑手太多，挤不进店门口", "老板说米饭还在蒸，让再等十分钟",
        "店员翻半天找不到我的单", "厨师请假，只剩一个人在炒",
        "外卖柜满了，店员在门口喊号", "出餐口堵着七八个骑手，谁也拿不到",
        "前面压了三张单，才轮到我", "现包现煮，慢是慢了点",
        "少打包一份，又回去等一轮",
    ],
    "merchant_fast": [
        "出一单给我一单，拎起来就走", "提前打包好放柜台上了",
        "店员一喊号我就拿到了", "老板直接从窗口递出来",
        "刚出锅，烫手，稳", "单子早做好了，盖着保温盖",
        "出餐口正好空着，一秒没等", "老板一边骂人一边炒，手挺快",
    ],
    "smooth": [
        "一路绿灯，一个红灯都没碰上", "抄了条没人走的小巷，近了八百米",
        "保安直接放行，连登记都免了", "电梯刚好在一楼，门还开着",
        "地图这次没坑人，直达楼下", "顾客提前下楼等着，一交接就走",
        "路上车少，油门踩得挺顺", "顺路把那单元的另一单也送了",
        "门口正好有个空位，车一停就上楼", "楼道灯全亮着，一路小跑上去",
        "小区门禁认识我了，直接开门", "雨没下大，全程没打湿",
    ],
    "grumpy": [
        "你还要多久？我饿得胃疼", "我都吃完一碗泡面了",
        "催一下，孩子饿哭了", "电话打三遍了，你在哪",
        "怎么还没到？我马上要出门", "我看着你在地图上绕圈",
        "先把我的单送了吧，求你了", "再不来我就点别家了",
        "等了快一小时，你人呢", "能不能快点，我赶时间",
        "你到哪了？我下楼等着了", "饭不要了，退了吧",
    ],
    "late": [
        "路口封了，绕了一大圈", "电梯坏在 18 楼，硬爬上去的",
        "赶上放学，学校门口全堵死", "导航把我带进死胡同",
        "车胎在桥下没气了", "保安不让进，登记排了十分钟",
        "红绿灯一个接一个，全赶上了", "雨太大，看不清路",
        "顾客电话一直占线，找不到门", "小区三个门，跑错了两个",
        "地下车库找不到出口", "路上追尾，等交警来处理",
        "顾客临时改地址，白跑一趟", "堵在高架上，一动不动",
        "手机没电，找充电宝找了半天", "电梯要刷卡，顾客不接电话",
    ],
    "fail": [
        "电话打了七个，一直没人接", "单元门禁按了半天，没人给开",
        "地址写的是 3 栋，实际是 3 号院，绕了半个城", "敲门没人应，隔着门听见手机在响",
        "餐箱倒了，汤全洒在袋子里，没法交", "保安拦在门口，说外卖不许上电梯",
        "顾客说在楼下等，绕了两圈没见到人", "到了门口发现是写字楼，前台早下班了",
        "顾客临时说不要了，让我退回商家", "下大雨，纸箱全湿透了，东西退回去",
        "顾客填的是旧地址，人已经搬走", "找错单元，敲开别人家门被骂了一顿",
        "车停路边被贴条，回头顾客已经取消", "手机导航显示到点了，实际是一片工地",
        "餐盒被压扁，顾客看了一眼直接拒收", "电梯故障停在半层，人在里面出不来",
    ],
    "fail_cust": [
        "算了，我退单了，别送了", "等一个小时了，我下楼随便吃点",
        "不用了，我已经在楼下买了", "那我饿着吧",
        "人都走完了，你送过来给谁吃", "钱退我，单我不接了",
        "你自己留着吃吧", "我这儿都散会了，不用送了",
        "白等一场，下次别接我这单", "东西退了吧，我赶时间出门了",
        "凉透了还送什么，退单", "行吧，我点别的了",
    ],
}


def _clean(s: str) -> str:
    s = str(s).strip().strip("\"'“”「」")
    # 只剥「1. 」「- 」「• 」这类列表标记，别把「24 小时药房」的 24 也吃掉
    s = re.sub(r"^[\s\-*·•]*(?:\d+[.、)）]\s*)?", "", s)
    s = s.replace("\n", " ").strip()
    return s[:48]


def _ok(s: str, kind: str = "") -> bool:
    lim = 26 if kind in ("note", "note_goods", "bulk") else 34
    low = 4
    if kind == "names":
        lim, low = 8, 3
    if not (low <= len(s) <= lim):
        return False
    if any(w in s for w in _BAD_WORDS):
        return False
    if kind == "shops":
        parts = [p.strip() for p in s.split(SHOP_SEP)]
        if len(parts) != 3 or not all(parts):
            return False
        if len(parts[0]) > 6 or len(parts[1]) > 16 or len(parts[2]) > 22:
            return False
        # 店名和品类至少得像同一行的：数码单不该是跑腿公司，跑腿单不该是药房
        name = parts[1]
        if parts[0] in ("数码", "五金", "建材", "服装", "宠物", "文具") and \
                any(w in name for w in ("跑腿", "闪送", "同城", "药房", "超市", "生鲜", "水果")):
            return False
        if parts[0] in ("跑腿", "洗衣") and any(w in name for w in ("药房", "超市", "数码", "五金", "建材")):
            return False
    return True


def _parse_obj(text: str) -> dict:
    """从模型输出里捞一个 JSON 对象（可能包着 ``` 或前后有废话）。"""
    t = (text or "").strip()
    if "```" in t:
        t = re.sub(r"```[a-zA-Z]*", "", t).replace("```", "")
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): v for k, v in obj.items() if isinstance(v, str)}


def parse_list(text: str, kind: str = "") -> list[str]:
    """从模型输出里尽量捞出字符串数组。"""
    t = (text or "").strip()
    if "```" in t:
        t = re.sub(r"```[a-zA-Z]*", "", t).replace("```", "")
    out: list[str] = []
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                out = [_clean(x) for x in arr if isinstance(x, (str, int, float))]
        except Exception:
            out = []
    if not out:
        out = [_clean(x) for x in re.split(r"[\n\r]+", t) if x.strip()]
    seen, res = set(), []
    for s in out:
        if _ok(s, kind) and s not in seen:
            seen.add(s)
            res.append(s)
    return res


class Narrator:
    """素材池。线程/协程安全靠单事件循环 + 单任务补货。"""

    def __init__(self, path: str):
        self.path = path
        self.pools: dict[str, list[str]] = {k: [] for k in KINDS}
        self._filling: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self.loaded = False
        self.last_error = ""      # 最近一次补货失败的原因，给 #叙事池 看
        self.last_ok = 0.0        # 最近一次补货成功的时间
        self.last_got = 0         # 最近一次补货拿到几条
        self.log = None           # 由 main 注入 logger.info，便于排查
        # 实时生成（每单现编剧情）的统计，给 #叙事池 看
        self._used: dict[str, int] = {k: 0 for k in KINDS}
        self.live_ok = 0
        self.live_fail = 0
        self.live_ms = 0
        self.live_err = ""

    def _log(self, msg: str) -> None:
        fn = self.log
        if fn is None:
            return
        try:
            fn(msg)
        except Exception:
            pass

    # ---------------------------------------------------------- 存取
    def load(self) -> None:
        self.loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for k in KINDS:
            got = data.get(k) or []
            if isinstance(got, list):
                self.pools[k] = [s for s in got if isinstance(s, str) and _ok(s, k)]

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.pools, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------------------------------------------------------- 取用
    def pick(self, kind: str, rng: random.Random) -> str:
        self._used[kind] = self._used.get(kind, 0) + 1
        pool = self.pools.get(kind) or []
        if pool:
            return rng.choice(pool)
        fb = FALLBACK.get(kind) or []
        return rng.choice(fb) if fb else ""

    def many_pool(self, kind: str) -> list[str]:
        """整个池子（含内置兜底）——要按品类过滤的调用方用这个。"""
        self._used[kind] = self._used.get(kind, 0) + 1
        return list(self.pools.get(kind) or []) or list(FALLBACK.get(kind) or [])

    def pick_many(self, kind: str, n: int, rng: random.Random) -> list[str]:
        pool = self.many_pool(kind)
        if not pool:
            return [""] * n
        if len(pool) >= n:
            return rng.sample(pool, n)
        return [rng.choice(pool) for _ in range(n)]

    # 差评类型 → 池子（迟到 / 洒凉 / 碰瓷，各说各的，不能串）
    BAD_POOL = {"late": "bad_late", "pack": "bad_pack",
                "damage": "bad_damage", "karen": "bad_karen"}

    # 非餐饮单（药、花、快递、数码、衣服）不能出现的字眼：
    # 药房里没人会夸你「汤没洒」，花店也不会说「筷子塞好了」
    FOOD_WORDS = ("汤", "酱", "筷", "辣", "香菜", "碗", "串", "粥", "粉", "面",
                  "饭", "菜", "餐", "炒", "煎", "炸", "烤", "锅", "奶茶", "汉堡",
                  "烧烤", "肉", "鱼", "水果", "盒饭", "打包盒", "饮品",
                  "冰", "糖", "味", "吃", "喝", "葱", "蒜", "椒", "醋", "油",
                  "蛋", "饼", "糕", "奶", "汁", "饿了", "胃")

    # 过滤后可能一条不剩，用这几句通用的话兜住（不带任何品类暗示）
    PLAIN = {
        "merchant": ["袋子口扎好了，小票塞在里面", "东西搁前台了，签一下字",
                     "包装重新缠了胶带，路上不会散", "店员核对了两遍才给我",
                     "已经在柜台放好了，报手机尾号就取",
                     "出库单贴好了，直接拎走"],
        "cust": ["包装一点没压坏，谢谢", "这么远还送这么快，辛苦了",
                 "还帮我带到门口，好评", "地址写得绕，他还是找到了",
                 "东西一路都没磕着，稳", "提前了十分钟，够意思"],
        "chat": ["还得多久啊", "我这边赶时间", "到门口给我打个电话",
                 "地址上写了三号楼，别跑错", "我下楼了，别上来"],
        "road": ["小区三个门都关了，绕了一大圈", "导航把我带到隔壁院了",
                 "电梯停了两趟才轮上", "楼下停满车，找位置花了十分钟",
                 "门禁打不通电话，等了半会儿", "这条路在修，全是坑，骑不快"],
        "road_ok": ["门口正好有位置停，省了时间", "保安直接放行，没登记",
                    "电梯在一楼等着", "一路绿灯，没等一个红灯",
                    "抄了条小巷，近了八百米", "收件人早早就在楼下等着"],
    }

    # 池名 → 兜底句的键（池名是 merchant_slow 这种，PLAIN 里只分几类）
    PLAIN_KEY = {"praise": "cust", "grumpy": "chat",
                 "late": "road", "smooth": "road_ok",
                 "merchant_slow": "merchant", "merchant_fast": "merchant",
                 "bad_late": "cust", "bad_pack": "cust",
                 "bad_damage": "cust", "bad_karen": "cust"}

    def _safe_pick(self, kind: str, food: bool, rng: random.Random) -> str:
        """非餐饮单避开菜品字眼；全被过滤光就用通用句。"""
        if food:
            return self.pick(kind, rng)
        pool = [x for x in self.many_pool(kind)
                if x and not any(w in x for w in self.FOOD_WORDS)]
        if pool:
            return rng.choice(pool)
        plain = self.PLAIN.get(self.PLAIN_KEY.get(kind, "cust")) or [""]
        return rng.choice(plain)

    def story(self, *, late: bool, slow: bool, bad_kind: str, praised: bool,
              rng: random.Random, food: bool = True, failed: bool = False) -> dict:
        """按结算结果挑剧情，保证「说法对得上账」。

        late=True 就必须给一条真能拖慢时间的事（而不是"一路绿灯"），
        slow=True 就必须给一条真能解释排队的原因，差评则按类型分池。
        failed=True 是配送失败：原因得是「没送到」而不是「晚了」。
        food=False 是非餐饮单（药/花/快递/数码/衣服），说法里不许出现汤菜饭。
        """
        out = {"cust": "", "merchant": "", "late": "", "chat": ""}
        if failed:
            # 失败了就没有「路上顺利」这种话，原因直接给「为什么没送到」
            out["late"] = self._safe_pick("fail", food, rng)
        if bad_kind:
            out["cust"] = self._safe_pick(self.BAD_POOL.get(bad_kind, "bad_pack"),
                                          food, rng)
        elif praised:
            out["cust"] = self._safe_pick("praise", food, rng)
        elif failed:
            # 顾客已经知道送不到了，说的是「算了」而不是「快点」
            out["cust"] = self._safe_pick("fail_cust", food, rng)
        elif not late and rng.random() < 0.30:
            # 超时的单不可能有人夸你准时，别自相矛盾
            out["cust"] = self._safe_pick("praise", food, rng)
        elif late and rng.random() < 0.60:
            # 超时但还没给差评：顾客在电话里催/骂两句
            out["chat"] = self._safe_pick("grumpy", food, rng)
        if slow:
            out["merchant"] = self._safe_pick("merchant_slow", food, rng)
        elif rng.random() < 0.45:
            out["merchant"] = self._safe_pick("merchant_fast", food, rng)
        if not failed:
            if late:
                out["late"] = self._safe_pick("late", food, rng)
            elif rng.random() < 0.5:
                out["late"] = self._safe_pick("smooth", food, rng)
        return out

    def sizes(self) -> dict[str, int]:
        return {k: len(self.pools.get(k) or []) for k in KINDS}

    def needs(self) -> list[str]:
        todo = [k for k in KINDS if len(self.pools.get(k) or []) < MIN_POOL]
        # 店名和称谓用久了会腻：取满 ROTATE_AFTER 次就再要一批（池子上限 240，会一直滚新的）
        for k in ("shops", "names"):
            if k not in todo and self._used.get(k, 0) >= ROTATE_AFTER:
                todo.append(k)
        return todo

    # ---------------------------------------------------------- 补货
    def ensure(self, provider) -> None:
        """池子不够就在后台补，绝不阻塞当前这一单。"""
        if provider is None or self._task is not None:
            return
        now = time.time()
        todo = [k for k in self.needs() if now - self._filling.get(k, 0) > COOLDOWN]
        if not todo:
            return
        for k in todo:
            self._filling[k] = now
        self._task = asyncio.create_task(self._refill_many(provider, todo))

    async def _refill_many(self, provider, kinds: list[str]) -> None:
        try:
            for k in kinds:
                await self._refill(provider, k)
        finally:
            self._task = None

    async def _refill(self, provider, kind: str) -> None:
        n = REFILL
        sys_prompt = (
            "你是一个中文生活场景素材生成器，为一个「外卖骑手」聊天小游戏提供素材。"
            f"请生成 {n} 条{SPEC[kind]}。"
            "要求：中文，口语化、有画面感、互不重复；"
            "每条都是一句直接用的话，**不要解释、不要分析、不要点评**；"
            "不要编号、不要加引号、不要写「因为…所以…」这种推理；"
            "每条不超过 24 个字；只输出一个 JSON 字符串数组。"
        )
        if kind == "shops":
            sys_prompt = (
                "你是一个中文生活场景素材生成器，为一个「外卖骑手」聊天小游戏提供店铺。"
                f"请生成 {n} 条{SPEC[kind]}。"
                "要求：每条都是 `品类|店名|商品` 三段；品类只用规定的那 16 个词；"
                "店名必须和品类对得上（数码类写小米之家、顺电、京东之家这种，"
                "跑腿类才写顺丰同城、闪送、美团跑腿）；商品必须这家店真的会卖。"
                "不要编号、不要解释；只输出一个 JSON 字符串数组。"
            )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=f"生成 {n} 条，只输出 JSON 数组。",
                                   system_prompt=sys_prompt),
                timeout=60,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.last_error = f"{kind}: {type(e).__name__}: {e}"
            self._log(f"[waimai] 叙事池补货失败 {self.last_error}")
            self._filling[kind] = time.time() - COOLDOWN + RETRY_AFTER
            return
        text = getattr(resp, "completion_text", "") or ""
        got = parse_list(text, kind)
        if not got:
            self.last_error = f"{kind}: 返回内容解析不出（{len(text)} 字）"
            self._log(f"[waimai] 叙事池补货失败 {self.last_error}")
            self._filling[kind] = time.time() - COOLDOWN + RETRY_AFTER
            return
        pool = self.pools.setdefault(kind, [])
        have = set(pool)
        for s in got:
            if s not in have:
                pool.append(s)
                have.add(s)
        if len(pool) > MAX_POOL:
            del pool[: len(pool) - MAX_POOL]
        self.last_error = ""
        self.last_ok = time.time()
        self.last_got = len(got)
        self._used[kind] = 0
        self.save()
        self._log(f"[waimai] 叙事池 +{len(got)} 条 → {kind}（共 {len(pool)}）")

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # ---------------------------------------------------------- 每单实时生成
    #
    # 池子是常态，但用户要的是"这一单现编的剧情"：结算时直接问模型一次，
    # 内容只跟这单的店/货/结果有关，不必跟池子里别的话重复。
    # 超时就退回池子——不能让群里等一个模型。

    LIVE_SYS = (
        "你在给一个中文外卖跑腿小游戏当编剧。玩家刚送完一单，你要写这一单发生的事。"
        "只输出一个 JSON 对象，四个键：\n"
        '{"merchant":"商家那边发生了什么，一句话","road":"路上遇到什么，一句话",'
        '"review":"顾客的评价，一句话","chat":"顾客催单的原话，一句话"}\n'
        "硬规则：\n"
        "1) 必须和这单的结果一致：准时送达的，路上不能有会耽误时间的事；"
        "没超时的单，road 写一句顺利的见闻；超时的单，road 必须是真能拖慢时间的事，"
        "并且 review（如果有）必须是在抱怨迟到。\n"
        "2) 每条不超过 20 个字，口语、具体、有画面感，不要解释、不要写「因为所以」，"
        "也别直接说「出餐慢」「准时」「超时」这种结论词，直接写看到的画面（谁在排队、锅在响、红灯、电梯）。\n"
        "3) 说的是什么货就要像什么：药要写药、花要写花、文件要写文件、吃的可以提汤和饭。\n"
        "4) review / chat 用不到就给空字符串。\n"
        "5) 别写政治、性别攻击、脏话。"
    )

    async def live_story(self, provider, *, who: str, biz: str, shop: str, item: str,
                         food: bool, late: bool, slow: bool, bad_kind: str,
                         praised: bool, timeout: float = 4.0,
                         failed: bool = False) -> dict | None:
        """现编这一单的剧情。失败/超时返回 None，调用方退回池子。"""
        if provider is None:
            return None
        scene = [
            f"店铺：{shop}（{biz or '未知品类'}）",
            f"货：{item or '普通包裹'}",
            f"收件人：{who or '顾客'}",
            "结果：" + ("这单没送到，已经退回商家了（配送失败）" if failed
                       else ("超时了" if late else "准时送达")),
            "出餐：" + ("商家出餐慢，等了一会儿" if slow else "出餐正常"),
        ]
        if bad_kind:
            label = {"late": "迟到", "pack": "洒了/凉了", "damage": "磕碰坏了",
                     "karen": "无理取闹"}.get(bad_kind, "不满意")
            scene.append(f"顾客给了差评，类型是「{label}」")
        elif praised:
            scene.append("顾客很满意，还给了好评")
        if not food:
            scene.append("注意：这单不是吃的（药/花/快递/数码/衣服/水果这类），"
                         "不要提汤、饭、菜、筷子、辣这些字")
        t0 = time.time()
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt="\n".join(scene), system_prompt=self.LIVE_SYS),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.live_fail += 1
            self.live_err = f"{type(e).__name__}: {e}"
            return None
        text = getattr(resp, "completion_text", "") or ""
        data = _parse_obj(text)
        if not data:
            self.live_fail += 1
            self.live_err = f"实时剧情解析不出（{len(text)} 字）"
            return None
        out = {"cust": "", "merchant": "", "late": "", "chat": ""}
        merchant = _clean(data.get("merchant", ""))[:30]
        road = _clean(data.get("road", ""))[:30]
        review = _clean(data.get("review", ""))[:30]
        chat = _clean(data.get("chat", ""))[:30]
        if not food:
            for k, v in (("m", merchant), ("r", road), ("v", review), ("c", chat)):
                if v and any(w in v for w in self.FOOD_WORDS):
                    if k == "m":
                        merchant = ""
                    elif k == "r":
                        road = ""
                    elif k == "v":
                        review = ""
                    else:
                        chat = ""
        # 自相矛盾的一律丢掉：准时单不许有拖慢路上的事，超时单不许被夸准时
        if not late and not failed and road and any(
                w in road for w in ("堵", "绕", "坏", "追尾", "找不到", "封")):
            road = ""
        if (late or failed) and review and not any(
                w in review for w in ("晚", "迟", "等", "慢", "凉", "冷", "没", "不")):
            review = ""
        if not chat or not (late or failed):
            chat = ""
        out["merchant"], out["late"], out["cust"], out["chat"] = merchant, road, review, chat
        if not any(out.values()):
            self.live_fail += 1
            self.live_err = "实时剧情四项全空"
            return None
        self.live_ok += 1
        self.live_ms = int((time.time() - t0) * 1000)
        self.live_err = ""
        return out