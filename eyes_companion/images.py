"""有界检查JPEG标记和尺寸；具体解码错误仍由模型接口如实返回。"""


def jpeg_size(data):
    # 微信留存照片可能在EOI之后附加标记；JPEG结束标记不必是文件最后两字节。
    if not data.startswith(b"\xff\xd8") or data.rfind(b"\xff\xd9") < 2:
        raise ValueError("图片不是完整的JPEG数据")
    offset = 2
    while offset + 3 < len(data):
        if data[offset] != 255:
            raise ValueError("JPEG标记损坏")
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9, 0xDA}:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            raise ValueError("JPEG片段长度无效")
        if marker in {0xC0, 0xC1, 0xC2}:
            if length < 8:
                raise ValueError("JPEG尺寸片段无效")
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            if not 1 <= width <= 4096 or not 1 <= height <= 4096:
                raise ValueError("图片宽高必须为1至4096像素")
            return width, height
        offset += length
    raise ValueError("没有读取到受支持的JPEG尺寸")
