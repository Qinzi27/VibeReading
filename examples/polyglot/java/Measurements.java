/** 本例仅声明计算方法；阅读器不需要编译或运行这个类。 */
public final class Measurements {
    /**
     * 用途：将一个测量值乘以给定系数。
     * 输入：value 为测量值，factor 为无量纲系数。
     * 输出：单位与 value 相同的缩放值。
     * 注意：调用方应事先确定系数的意义和数值范围。
     */
    public static double scaleValue(double value, double factor) {
        return value * factor;
    }

    /**
     * 用途：分别缩放两个测量值，再求和。
     * 输入：同一单位的 left、right，以及共同的 factor。
     * 输出：缩放后的总量。
     * 注意：两项能否相加需要领域依据。
     */
    public static double totalScaled(double left, double right, double factor) {
        return scaleValue(left, factor) + scaleValue(right, factor);
    }

    /**
     * 用途：比较缩放后的总量和参考值。
     * 输入：left、right、factor，以及与总量同单位的 reference。
     * 输出：总量减参考值。
     * 注意：差值不是统计显著性检验。
     */
    public static double compareTotal(double left, double right, double factor, double reference) {
        return totalScaled(left, right, factor) - reference;
    }
}
