"""跨文件调用示例；导入两个同名函数时，为标签函数使用显式别名。"""

from geometry import norm
from labels import norm as label_norm


def analyse_point(label: str, x: float, y: float) -> dict:
    """用途：把点的规范化标签与距原点的长度放在同一条记录中。

    输入：标签 label，以及同一单位下的平面坐标 x、y。
    输出：包含 label 和 distance 字段的字典。
    注意：调用的是两个不同文件里的 norm；没有读取或保存文件。
    """
    return {"label": label_norm(label), "distance": norm(x, y)}
