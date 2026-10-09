/// 用途：将一个测量值乘以给定系数。
/// 输入：value 是测量值，factor 是无量纲系数。
/// 输出：与 value 单位相同的缩放值。
/// 注意：示例不处理非有限值。
pub fn scale_value(value: f64, factor: f64) -> f64 {
    value * factor
}

/// 用途：分别缩放两个测量值，再求和。
/// 输入：同单位的 left、right，以及共同的 factor。
/// 输出：缩放后的总量。
/// 注意：两项能否相加需要领域依据。
pub fn total_scaled(left: f64, right: f64, factor: f64) -> f64 {
    scale_value(left, factor) + scale_value(right, factor)
}

/// 用途：比较缩放后的总量与参考值。
/// 输入：left、right、factor 和与总量同单位的 reference。
/// 输出：总量减参考值。
/// 注意：差值不是统计显著性检验。
pub fn compare_total(left: f64, right: f64, factor: f64, reference: f64) -> f64 {
    total_scaled(left, right, factor) - reference
}
