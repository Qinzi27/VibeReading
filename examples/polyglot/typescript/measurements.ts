/**
 * 用途：将一个测量值乘以给定系数。
 * 输入：数值 value 和无量纲系数 factor。
 * 输出：与 value 单位相同的缩放值。
 * 注意：number 类型本身不会拒绝 NaN 或无穷大。
 */
function scaleValue(value: number, factor: number): number {
  return value * factor;
}

/**
 * 用途：分别缩放两个测量值，再求和。
 * 输入：同单位的 left、right，以及共同的 factor。
 * 输出：缩放后的总量。
 * 注意：两项能否相加需要领域依据。
 */
function totalScaled(left: number, right: number, factor: number): number {
  return scaleValue(left, factor) + scaleValue(right, factor);
}

/**
 * 用途：比较缩放后的总量与参考值。
 * 输入：left、right、factor 和与总量同单位的 reference。
 * 输出：总量减参考值。
 * 注意：差值不是统计显著性检验。
 */
function compareTotal(left: number, right: number, factor: number, reference: number): number {
  return totalScaled(left, right, factor) - reference;
}
