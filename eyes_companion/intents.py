"""处理明确的设备意图；保留否定和改口，不用单字相似度猜操作。"""

import re


CAMERA_OFF = re.compile(
    r"(?:^|[，,。；;])\s*(?:请|现在|先)?\s*"
    r"(?:(?:不要|别|不用|不必)(?:再)?(?:打开|开启|使用|用)?(?:摄像头|相机|镜头)"
    r"|(?:关闭|关掉|停用)(?:摄像头|相机)|退出看景|停止看景|别看了)"
)


def camera_disabled(text):
    return bool(CAMERA_OFF.search(text))


def needs_vision(text, active=False):
    if camera_disabled(text):
        return False
    # 词语解释和看新闻不是取景。指向实物的问题仍可以请求当前画面。
    physical_target = bool(re.search(
        r"眼前(?:的|这|有)|镜头(?:里|中|前)|这(?:是什么意思|什么意思)|(?:这个|那个|这件|那件|手里|手上)"
        r"|(?:包装|盒子|瓶子|袋子)上", text
    ))
    if not physical_target and re.search(r"(?:是什么意思|是什么含义|如何定义|怎么理解|怎么计算|有什么区别)", text):
        return False
    if not physical_target and re.search(
            r"(?:看看|看一下|聊聊|讲讲|说说|听听|播放).*(?:新闻|天气|哲学|心理学|笑话|故事)", text):
        return False
    if re.search(r"(?:换个话题|聊点别的|不看东西|只聊天|继续聊天)", text):
        return False
    if re.search(
        r"(?:看看|看一下|看一看|读一下|念一下|识别一下).*(?:这个|那个|这里|那里|眼前|手里|上面|文字|日期)"
        r"|眼前|前面有什么|面前有什么|这是什么|这个是什么|这上面|包装上|生产日期|保质期", text
    ):
        return True
    if re.fullmatch(r"(?:请)?(?:帮我)?(?:看看|看一下|看一看)[。！？!?]*", text.strip()):
        return True
    # 已进入视觉交流的追问沿用模式，但仍由会话层重新索取有效画面。
    return active


def memory_change(text):
    match = re.fullmatch(r"(?:请)?(记住|把刚才(?:那条)?记忆改成)[，,:：\s]*(.+)", text.strip())
    if not match:
        return None
    content = match.group(2).strip()
    # 询问是否保存、询问词义以及同一句内撤回，都不能变成数据库写入。
    if re.search(r"(?:[？?]|吗|么|了没|没有|是什么意思)[。！？!?？\s]*$", content):
        return None
    if re.search(r"(?:^|[，,。；;])\s*(?:算了|别记了|不要保存|别保存|先不记|取消)", content):
        return None
    return ("remember" if match.group(1) == "记住" else "correct", content)
