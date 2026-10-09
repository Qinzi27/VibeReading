"""标签示例：函数名与 geometry.norm 相同，但输入输出和作用不同。"""


def norm(label: str) -> str:
    """用途：去掉标签两端的空白并统一为小写。

    输入：一个字符串标签。
    输出：规范化后的字符串。
    注意：不修改标签内部的空白，也不验证标签是否存在。
    """
    return label.strip().lower()
