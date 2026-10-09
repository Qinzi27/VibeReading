#' 用途：将一个测量值乘以给定系数。
#' 输入：数值 value 与无量纲系数 factor。
#' 输出：缩放后的数值，单位与 value 相同。
#' 注意：示例没有缺失值策略；NA 会按 R 的普通运算规则传播。
scale_value <- function(value, factor) {
  value * factor
}

#' 用途：分别缩放两个测量值，再求和。
#' 输入：同一单位的 left、right，以及共同的 factor。
#' 输出：缩放后两项的总和。
#' 注意：必须由调用方确认两项可以相加。
total_scaled <- function(left, right, factor) {
  scale_value(left, factor) + scale_value(right, factor)
}

#' 用途：比较缩放后的总量和参考值。
#' 输入：left、right、factor，以及与总量同单位的 reference。
#' 输出：总量减参考值；正值表示高于参考值。
#' 注意：差值不是统计显著性检验。
compare_total <- function(left, right, factor, reference) {
  total_scaled(left, right, factor) - reference
}
